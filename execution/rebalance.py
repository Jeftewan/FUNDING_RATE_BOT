"""Rebalanceo spot → margen (reports/profit_study_20261117.md, variante R2).

Cuando el mark recorre REBALANCE_FRACTION del camino ancla → SL, se vende la ganancia
del spot (fracción f = 1 − ancla/mark) y se recompra la misma fracción del short, en un
cierre parcial pareado; los USDT de la venta pasan al margen aislado del short. El
nocional queda igual a precios nuevos y la liquidación se re-ancla al precio del
rebalanceo: liq = p·(1 + 1/L)/(1 + mmr), como si se entrara a p con el mismo leverage.

Etapas (job `rebalance`):
  1. cierre parcial de f (execute_close: vende spot + recompra short, seguros cancelados);
  2. seguros otra vez con ancla p (sobre la liquidación ya mejorada por la recompra);
  3. BSC: USDT de la wallet → dirección de depósito Binance; esperar el crédito;
  4. spot → futuros (Universal Transfer) y margen aislado;
  5. seguros re-colocados sobre la liquidación final.
La recompra del short va con la venta: la seguridad no depende de la latencia del
depósito, solo el capital. Si el depósito no acredita, la posición queda más segura que
antes y el USDT en tránsito → alerta para terminar a mano (sin reintentos a ciegas).
"""
import logging
import os
import time

log = logging.getLogger("bot.rebalance")

MIN_USD = 10.0               # venta mínima: debajo, el gas y las comisiones pesan más
MAX_FRACTION = 0.5
COOLDOWN = 30 * 60           # entre rebalanceos (o intentos fallidos) de la misma posición
DEPOSIT_TIMEOUT = 20 * 60    # el estudio gana con ≤ 15 min de latencia; más allá, alerta
DEPOSIT_POLL = 15


def enabled() -> bool:
    return os.environ.get("REBALANCE_ENABLED", "0") == "1"


def rebalance_fraction() -> float:
    try:
        return float(os.environ.get("REBALANCE_FRACTION", "0.6"))
    except ValueError:
        return 0.6


def anchor(pos: dict, prot: dict) -> float:
    return float(prot.get("anchor") or prot.get("entry_price") or pos.get("entry_price") or 0)


def trigger_level(pos: dict, prot: dict):
    a, sl = anchor(pos, prot), float(prot.get("sl_price") or 0)
    if not a or not sl or sl <= a:
        return None
    return a + rebalance_fraction() * (sl - a)


def fraction(pos: dict, prot: dict, mark: float) -> float:
    a = anchor(pos, prot)
    if not a or not mark or mark <= a:
        return 0.0
    return min(MAX_FRACTION, 1 - a / mark)


def due(pos: dict, prot: dict, mark, now: float = None) -> bool:
    """¿Toca rebalancear? Solo con seguros activos, ambas piernas y fuera del cooldown."""
    now = now or time.time()
    if not enabled() or prot.get("status") != "active" or not mark:
        return False
    if not float(pos.get("spot_qty") or 0) or not float(pos.get("perp_qty") or 0):
        return False
    if now - float(prot.get("rebalance_at") or 0) < COOLDOWN:
        return False
    lvl = trigger_level(pos, prot)
    if lvl is None or mark < lvl:
        return False
    return fraction(pos, prot, mark) * float(pos["spot_qty"]) * mark >= MIN_USD


def _fills(pf: dict) -> float:
    pf = pf or {}
    return float(pf.get("perp_filled") or 0) + float(pf.get("spot_filled") or 0)


def _record(runner, pos_id, entry: dict):
    from core.database import db
    from core.db_models import UserPosition
    with runner.app.app_context():
        row = UserPosition.query.get(int(pos_id))
        if row:
            row.payments_json = list(row.payments_json or []) + [{"ts": time.time(), "kind": "rebalance",
                                                                   **entry}]
            db.session.commit()


def _replace_protection(runner, uid, pos_id, creds, wallet, token, anc) -> bool:
    """Cancela los seguros vivos y los vuelve a poner con ancla `anc`. Si al cancelar
    alguno ya se había llenado, no toca nada: el vigilante cierra en su próxima vuelta."""
    from exchanges import trade_executor as texec
    pos = runner._load_pos(pos_id)
    if not pos:
        return False
    prot = pos.get("protection") or {}
    if prot.get("status") == "active" and "open" in ((prot.get("perp") or {}).get("status"),
                                                     (prot.get("spot") or {}).get("status")):
        pf = texec.cancel_protection(creds, pos, wallet)
        if _fills(pf):
            log.warning(f"rebalance pos={pos_id}: un seguro se llenó al re-anclar; lo cierra el vigilante")
            return False
    prot = runner._protect(uid, pos_id, creds, token=token, anchor=anc)
    if prot is not None:
        runner._save_protection(pos_id, {**prot, "rebalance_at": time.time()})
    return bool(prot and prot.get("ok"))


def run(runner, job):
    from exchanges import trade_executor as texec
    from exchanges.binance_account import BinanceAccount
    jid, uid, p = job["id"], job["user_id"], job["params"]
    pos = runner._load_pos(p["position_id"])
    if not pos:
        runner._update(jid, status="failed", error="Posición no encontrada")
        return
    pos_id, sym = pos["id"], pos.get("symbol", "")
    f = float(p["fraction"])
    creds = runner._creds(uid, [pos.get("exchange", "")])
    bsc = pos.get("spot_venue") == "bsc"
    wallet, token = runner._bsc_args(uid, pos, p.get("token"))
    stages = {"started": time.time(), "fraction": f, "mark": p.get("mark")}

    # 1. Cierre parcial pareado de la fracción f.
    part = {**pos, "spot_qty": float(pos["spot_qty"]) * f, "perp_qty": float(pos["perp_qty"]) * f}
    res = texec.execute_close(creds, part, dry_run=False, ctx=runner._ctx(jid),
                              twap={"slices": 1, "duration_s": 0},
                              spot_leg_factory=runner._spot_leg_factory(
                                  uid, {"spot_venue": pos.get("spot_venue"), "token": token}))
    fees = float(res.get("exit_fees_usd") or 0)
    # `complete` no sirve aquí: en un cierre parcial la recompra redondea al paso del perp y
    # queda bajo f·perp_qty por menos de un paso. Fallo = error/cancelación o un seguro llenado.
    if not res.get("ok") or res.get("partial_note") or _fills(res.get("protection_filled")):
        if res.get("spot_sold") or res.get("perp_closed") or _fills(res.get("protection_filled")):
            runner._apply_partial_close(pos, {**res, "partial_note": f"Rebalanceo incompleto: {res.get('msg', '')}"},
                                        add_fees=fees)
        if not _fills(res.get("protection_filled")):
            runner._restore_protection(uid, runner._load_pos(pos_id) or pos, creds, token)
        msg = res.get("msg") or res.get("partial_note") or "un seguro se llenó durante el rebalanceo"
        runner._update(jid, status="failed", result_json=_jsonable(res), error=msg)
        _record(runner, pos_id, {"job_id": jid, "stage": "sell", "ok": False, "error": msg, **stages})
        runner._alert(uid, sym, f"Rebalanceo de {sym} no se completó: {msg}. Revisa la posición.",
                      severity="WARNING", typ="REBALANCE_FAILED")
        return
    runner._apply_partial_close(pos, {**res, "partial_note": f"Rebalanceo spot→margen ({f:.1%})"},
                                add_fees=fees)
    anc = float(res.get("perp_exit_price") or p.get("mark") or 0)
    usd = round(float(res.get("spot_exit_price") or 0) * float(res.get("spot_sold") or 0), 6)
    stages.update(sold=time.time(), anchor=anc, usd=usd, spot_sold=res.get("spot_sold"),
                  perp_closed=res.get("perp_closed"), fees_usd=fees, price_pnl=res.get("price_pnl"))

    # 2. Seguros ya con el ancla nueva (la recompra mejoró la liquidación real).
    _replace_protection(runner, uid, pos_id, creds, wallet, token, anc)

    # 3–4. USDT al margen aislado.
    acct = BinanceAccount(creds["binance"])
    amount = 0.0
    try:
        if bsc:
            from exchanges import onchain_bsc as ob
            have = ob.from_wei(ob.erc20_balance(ob.USDT, wallet["address"]), ob.USDT_DECIMALS)
            amount = int(min(usd, have) * 100) / 100
            if amount < MIN_USD / 2:
                raise RuntimeError(f"USDT en la wallet insuficiente ({have:.2f})")
            since_ms = int(time.time() * 1000) - 60_000
            addr = acct.deposit_address("USDT", "BSC")
            txh = ob.transfer_erc20(wallet, ob.USDT, addr, ob.to_wei(amount, ob.USDT_DECIMALS))
            stages.update(sent=time.time(), deposit_tx=txh)
            acct.wait_deposit(txh, since_ms, DEPOSIT_TIMEOUT, DEPOSIT_POLL)
            stages["credited"] = time.time()
        else:
            amount = int(usd * 100) / 100
        t = acct.transfer_usdt(amount, "future")
        if not t.get("ok"):
            raise RuntimeError(t.get("msg"))
        texec.add_isolated_margin(creds, runner._load_pos(pos_id) or pos, amount)
        stages.update(margin=time.time(), margin_usd=amount)
    except Exception as e:
        where = "en tránsito/spot de Binance" if stages.get("sent") else "en la wallet BSC" if bsc else "en spot"
        msg = f"{type(e).__name__}: {e}"
        stages.update(error=msg, amount=amount)
        _record(runner, pos_id, {"job_id": jid, "stage": "margin", "ok": False, **stages})
        runner._update(jid, status="failed", result_json=_jsonable({**res, "rebalance": stages}), error=msg)
        runner._alert(uid, sym, f"Rebalanceo de {sym}: spot vendido y short reducido, pero el margen NO se "
                                f"agregó ({msg}). {amount:.2f} USDT quedan {where}: pásalos al margen aislado "
                                f"a mano.", severity="WARNING", typ="REBALANCE_FAILED")
        return

    # 5. Seguros sobre la liquidación final.
    _replace_protection(runner, uid, pos_id, creds, wallet, token, anc)
    stages["done"] = time.time()
    lat = stages["margin"] - stages["sold"]
    _record(runner, pos_id, {"job_id": jid, "stage": "done", "ok": True, "latency_s": round(lat, 1), **stages})
    runner._update(jid, status="done", result_json=_jsonable({**res, "rebalance": stages}))
    runner._alert(uid, sym, f"Rebalanceo de {sym}: vendido {f:.1%} del spot ({usd:.2f} USD) y recomprado el "
                            f"mismo tanto del short; {amount:.2f} USDT al margen en {lat / 60:.1f} min. "
                            f"Ancla {anc:g}.", severity="INFO", typ="REBALANCED")


def _jsonable(obj):
    from execution.jobs import _jsonable as j
    return j(obj)
