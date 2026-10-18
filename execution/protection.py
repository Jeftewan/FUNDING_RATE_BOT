"""Vigilante de seguros spot_perp (SL del short + venta límite del spot).

Cada PROTECTION_INTERVAL s, por posición activa con protection_json.status=active:
  - Si se llenó el SL o la venta límite → lanza un job de cierre normal
    (JobRunner): cancela el seguro restante y cierra la pierna que siga abierta
    a mercado (el SL se dispara con mark price y la venta con el último precio,
    pueden no coincidir). El job registra fills/fees reales y cierra en la DB.
  - Orden límite BSC (Kyber, expira a 7 días) a < RENEW_BEFORE de expirar o ya
    expirada → se renueva y se avisa por Telegram.
  - Seguro cancelado fuera de basyo → alerta (una vez) y se marca cancelado.
"""
import logging
import threading
import time
from datetime import datetime

log = logging.getLogger("bot.protection")

PROTECTION_INTERVAL = 60
RENEW_BEFORE = 12 * 3600


class ProtectionWatcher:
    def __init__(self, runner):
        self.runner = runner
        self.app = runner.app
        self._started = False

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
        from core.db_models import UserPosition
        from core.db_persistence import DBPersistence
        with self.app.app_context():
            rows = UserPosition.query.filter(UserPosition.status == "active",
                                             UserPosition.protection_json.isnot(None)).all()
            dbp = DBPersistence()
            return [(r.user_id, dbp._pos_to_dict(r)) for r in rows
                    if (r.protection_json or {}).get("status") == "active"]

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
            if r.active_job(uid):
                return   # un job (apertura/cierre) en curso ya maneja la posición
        sym = pos.get("symbol", "")
        prot = dict(pos.get("protection") or {})
        creds = r._creds(uid, [pos.get("exchange", "")])
        wallet, token = r._bsc_args(uid, pos)
        st = texec.protection_status(creds, pos, wallet)

        if "filled" in (st["perp"], st["spot"]):
            which = "SL del short" if st["perp"] == "filled" else "venta límite del spot"
            log.info(f"protection fired {sym} pos={pos['id']}: {which} (perp={st['perp']} spot={st['spot']})")
            with self.app.app_context():
                if r.active_job(uid):
                    return
            # Un solo intento automático: si el cierre falla, su alerta CRITICAL pide
            # revisión manual (no reintentar cada minuto).
            r._save_protection(pos["id"], {**prot, "status": "triggered", "triggered_at": time.time(),
                                           "triggered_by": "perp" if st["perp"] == "filled" else "spot"})
            with self.app.app_context():
                jid = r.submit(uid, "close", {"position_id": int(pos["id"]), "symbol": sym, "twap": None,
                                              "token": token or {}, "trigger": "protection"})
            r._alert(uid, sym, f"Seguro ejecutado en {sym}: {which} a {prot.get('sl_price'):g}. "
                               f"Cerrando la posición (job #{jid}).", severity="WARNING",
                     typ="PROTECTION_TRIGGERED")
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
