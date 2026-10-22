"""Vigilante de seguros spot_perp (SL del short + venta límite del spot).

Cada PROTECTION_INTERVAL s, por posición activa con protection_json.status=active:
  - Si se llenó el SL o la venta límite → lanza un job de cierre normal
    (JobRunner): cancela el seguro restante y cierra la pierna que siga abierta
    a mercado (el SL se dispara con mark price y la venta con el último precio,
    pueden no coincidir). El job registra fills/fees reales y cierra en la DB.
  - Orden límite BSC (Kyber, expira a 7 días) a < RENEW_BEFORE de expirar o ya
    expirada → se renueva y se avisa por Telegram.
  - Seguro cancelado fuera de basyo → alerta (una vez) y se marca cancelado.
  - Short desaparecido con spot en manos (liquidación / cierre externo) → alerta
    CRITICAL (cada NAKED_REALERT s) y, solo con AUTO_UNWIND_ON_LIQ=1, cierre del
    spot. Short cerca de liquidación (< LIQ_WARN_PCT) → alerta.

Se revisa cada posición aunque el usuario tenga OTRO job en curso (un TWAP de
horas en otro símbolo no puede dejar sin vigilancia a esta); solo se salta la
posición si un job activo es de ella misma.
"""
import logging
import os
import threading
import time
from datetime import datetime

log = logging.getLogger("bot.protection")

PROTECTION_INTERVAL = 60
RENEW_BEFORE = 12 * 3600
LIQ_WARN_PCT = 20.0          # distancia mark→liquidación del short que dispara aviso
LIQ_REALERT = 6 * 3600
NAKED_REALERT = 3600


def _auto_unwind() -> bool:
    return os.environ.get("AUTO_UNWIND_ON_LIQ", "0") == "1"


class ProtectionWatcher:
    def __init__(self, runner):
        self.runner = runner
        self.app = runner.app
        self._started = False
        self._alerted = {}       # (pos_id, kind) → ts del último aviso (dedup en memoria)

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="protection-watch").start()

    def _loop(self):
        time.sleep(45)
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception(f"protection loop: {e}")
            time.sleep(PROTECTION_INTERVAL)

    def _positions(self):
        """Posiciones spot_perp activas en Binance con cantidades registradas
        (auto/importadas): las que tienen seguros y las que no (las sin seguros
        también necesitan la vigilancia de short desnudo / liquidación)."""
        from core.db_models import UserPosition
        from core.db_persistence import DBPersistence
        from exchanges.trade_executor import PROTECTION_EXCHANGES
        with self.app.app_context():
            rows = UserPosition.query.filter(UserPosition.status == "active").all()
            dbp = DBPersistence()
            out = []
            for r in rows:
                if (r.mode or "spot_perp") != "spot_perp" or (r.exchange or "").lower() \
                        not in PROTECTION_EXCHANGES or r.source not in ("auto", "imported"):
                    continue
                out.append((r.user_id, dbp._pos_to_dict(r)))
            return out

    def tick(self):
        for uid, pos in self._positions():
            try:
                self.check(uid, pos)
            except Exception as e:
                log.warning(f"protection check pos={pos.get('id')} user={uid}: {e}")

    def check(self, uid, pos):
        from exchanges import trade_executor as texec
        r = self.runner
        with self.app.app_context():
            if r.active_job_for_position(uid, pos.get("id")):
                return   # un job de ESTA posición (cierre/reconciliación) ya la maneja
        sym = pos.get("symbol", "")
        prot = dict(pos.get("protection") or {})
        creds = r._creds(uid, [pos.get("exchange", "")])
        wallet, token = r._bsc_args(uid, pos)
        if prot.get("status") == "active":
            st = texec.protection_status(creds, pos, wallet)
        else:
            st = {"perp": "missing", "spot": "missing"}

        if "filled" in (st["perp"], st["spot"]):
            which = "SL del short" if st["perp"] == "filled" else "venta límite del spot"
            log.info(f"protection fired {sym} pos={pos['id']}: {which} (perp={st['perp']} spot={st['spot']})")
            # Un solo intento automático: si el cierre falla, su alerta CRITICAL pide
            # revisión manual (no reintentar cada minuto). Se lanza aunque el usuario
            # tenga otro job: la otra pierna está descubierta.
            r._save_protection(pos["id"], {**prot, "status": "triggered", "triggered_at": time.time(),
                                           "triggered_by": "perp" if st["perp"] == "filled" else "spot"})
            with self.app.app_context():
                jid = r.submit(uid, "close", {"position_id": int(pos["id"]), "symbol": sym, "twap": None,
                                              "token": token or {}, "trigger": "protection"})
            r._alert(uid, sym, f"Seguro ejecutado en {sym}: {which} a {prot.get('sl_price'):g}. "
                               f"Cerrando la posición (job #{jid}).", severity="WARNING",
                     typ="PROTECTION_TRIGGERED")
            return

        if self._check_short(uid, pos, creds, token):
            return
        if prot.get("status") != "active":
            return

        changed = False
        spot = dict(prot.get("spot") or {})
        if spot.get("venue") == "bsc" and spot.get("status") == "open" and wallet and token:
            left = float(spot.get("expires_at") or 0) - time.time()
            if (st["spot"] == "open" and left < RENEW_BEFORE) or (st["spot"] == "canceled" and left <= 0):
                prot["spot"] = self._renew_bsc(uid, sym, wallet, token, spot, prot["sl_price"],
                                               cancel_old=st["spot"] == "open")
                changed = True
                st["spot"] = "renewed"

        for leg, label in (("perp", "El SL del short"), ("spot", "La venta límite del spot")):
            cur = dict(prot.get(leg) or {})
            if st[leg] == "canceled" and cur.get("status") == "open":
                cur["status"] = "canceled"
                prot[leg] = cur
                changed = True
                r._alert(uid, sym, f"{label} de {sym} fue cancelado fuera de basyo: la posición "
                                   f"queda sin ese seguro.", severity="WARNING", typ="PROTECTION_CANCELED")
        if changed:
            r._save_protection(pos["id"], prot)

    def _check_short(self, uid, pos, creds, token) -> bool:
        """Short desnudo / cerca de liquidación. True si lanzó un cierre."""
        from exchanges import trade_executor as texec
        r = self.runner
        sym = pos.get("symbol", "")
        if not float(pos.get("perp_qty") or 0) or not float(pos.get("spot_qty") or 0):
            return False
        h = texec.short_health(creds, pos)
        now = time.time()
        if h["short"] is None:
            return False                       # lectura fallida: no concluir nada
        if h["short"] < float(pos["perp_qty"]) * 0.05:
            if now - self._alerted.get((pos["id"], "naked"), 0) < NAKED_REALERT:
                return False
            self._alerted[(pos["id"], "naked")] = now
            if _auto_unwind():
                with self.app.app_context():
                    jid = r.submit(uid, "close", {"position_id": int(pos["id"]), "symbol": sym,
                                                  "twap": None, "token": token or {},
                                                  "trigger": "naked_spot"})
                r._alert(uid, sym, f"El short de {sym} ya no existe (¿liquidado?) y el spot quedó "
                                   f"descubierto: vendiendo el spot (job #{jid}).", typ="NAKED_SPOT")
                return True
            r._alert(uid, sym, f"El short de {sym} ya no existe (¿liquidado o cerrado fuera de basyo?) "
                               f"y tienes {float(pos['spot_qty']):g} en spot SIN cobertura. Cierra el spot "
                               f"o re-abre el short YA.", typ="NAKED_SPOT")
            return False
        dist = h.get("liq_distance_pct")
        if dist is not None and dist < LIQ_WARN_PCT \
                and now - self._alerted.get((pos["id"], "liq"), 0) >= LIQ_REALERT:
            self._alerted[(pos["id"], "liq")] = now
            r._alert(uid, sym, f"El short de {sym} está a {dist:.1f}% de liquidación (mark {h['mark']:g}, "
                               f"liq {h['liq']:g}). Agrega margen aislado o reduce la posición.",
                     severity="WARNING", typ="LIQUIDATION_RISK")
        return False

    def _renew_bsc(self, uid, sym, wallet, token, spot, sl, cancel_old=True) -> dict:
        from exchanges import kyber_limit_order as klo
        from exchanges import onchain_bsc as bsc
        try:
            if cancel_old:
                klo.cancel(wallet, [spot["id"]])
            held = bsc.from_wei(bsc.erc20_balance(token["address"], wallet["address"]), int(token["decimals"]))
            qty = min(float(spot.get("qty") or 0), held)
            lo = klo.place_sell(wallet, token, qty, sl)
            until = datetime.fromtimestamp(lo["expires_at"]).strftime("%Y-%m-%d %H:%M")
            self.runner._alert(uid, sym, f"Orden límite de venta {sym} (BSC) renovada a {sl:g} "
                                         f"hasta {until}.", severity="INFO", typ="PROTECTION_RENEWED")
            return {**spot, "id": lo["id"], "status": "open", "qty": qty, "expires_at": lo["expires_at"],
                    "renewed_at": time.time()}
        except Exception as e:
            log.error(f"renew bsc limit {sym}: {e}")
            self.runner._alert(uid, sym, f"No se pudo renovar la orden límite de venta {sym} (BSC): {e}. "
                                         f"Ponla manualmente.")
            return {**spot, "status": "canceled" if cancel_old else spot.get("status"),
                    "renew_error": str(e)}
