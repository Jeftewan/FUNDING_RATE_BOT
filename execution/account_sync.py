"""Sync periódico de cuentas reales (Binance + wallet BSC) → DB.

Cada SYNC_INTERVAL s, para cada usuario con keys de Binance:
  1. snapshot de cuenta (saldos, posiciones, permisos, modo, importables, drift)
     → user_account_snapshots (alimenta el capital REAL del usuario).
  2. earnings REALES: income FUNDING_FEE por posición Binance auto/importada
     → reemplaza el estimado (earnings_source='real').
  3. alertas POSITION_DRIFT (dedup diaria).
Wallet BSC (si existe): saldos USDT/BNB/tokens → snapshot 'bsc_wallet'.
"""
import logging
import threading
import time
from datetime import datetime

log = logging.getLogger("bot.sync")

SYNC_INTERVAL = 300


class AccountSyncer:
    def __init__(self, app, scanner_worker):
        self.app = app
        self.worker = scanner_worker
        self._started = False
        self._lock = threading.Lock()
        self._user_locks = {}
        self._wake = threading.Event()

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="account-sync").start()
        log.info("Account sync started")

    def request(self):
        self._wake.set()

    def _loop(self):
        time.sleep(20)
        while True:
            try:
                self.sync_all()
            except Exception as e:
                log.exception(f"account sync loop: {e}")
            self._wake.wait(SYNC_INTERVAL)
            self._wake.clear()

    def sync_all(self):
        from core.db_persistence import DBPersistence
        with self.app.app_context():
            uids = set(DBPersistence().users_with_exchange_keys("binance"))
            from core.db_models import UserWallet
            uids |= {w.user_id for w in UserWallet.query.all()}
        for uid in uids:
            try:
                self.sync_user(uid)
            except Exception as e:
                log.warning(f"sync user {uid} failed: {e}")

    def _ulock(self, uid):
        with self._lock:
            return self._user_locks.setdefault(uid, threading.Lock())

    def sync_user(self, uid: int) -> dict:
        """Sync completo de un usuario. Devuelve el snapshot de Binance (o {})."""
        with self._ulock(uid):
            out = {}
            snap = self._sync_binance(uid)
            if snap is not None:
                out = snap
            self._sync_wallet(uid)
            return out

    # ── Binance ────────────────────────────────────────────────────────
    def _sync_binance(self, uid: int):
        from core.db_persistence import DBPersistence
        from exchanges.binance_account import BinanceAccount
        with self.app.app_context():
            dbp = DBPersistence()
            creds = dbp.load_user_exchange_keys(uid, "Binance") \
                or dbp.load_user_exchange_keys(uid, "binance")
            if not creds:
                return None
            positions = dbp.load_user_state(uid)["positions"]
        acct = BinanceAccount(creds)
        try:
            snap = acct.snapshot(positions)
        except Exception as e:
            log.warning(f"binance snapshot user={uid}: {e}")
            with self.app.app_context():
                DBPersistence().save_account_snapshot(uid, "binance", {}, ok=False,
                                                      error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}

        with self.app.app_context():
            DBPersistence().save_account_snapshot(uid, "binance", snap, ok=True)

        for pos in positions:
            if (pos.get("exchange") or "").lower() != "binance" or pos.get("mode") != "spot_perp":
                continue
            if pos.get("source") not in ("auto", "imported"):
                continue
            try:
                self.update_real_earnings(acct, pos)
            except Exception as e:
                log.warning(f"real earnings {pos.get('symbol')} user={uid}: {e}")

        self._drift_alerts(uid, snap.get("drift") or [])
        return snap

    def update_real_earnings(self, acct, pos: dict):
        """earned_real = Σ FUNDING_FEE del símbolo desde la entrada (income real)."""
        from core.database import db
        from core.db_models import UserPosition
        since_ms = int(pos.get("entry_time") or 0)
        incomes = acct.funding_income(pos["symbol"], since_ms)
        exposure = float(pos.get("exposure") or 0) or 1.0
        # Se reemplazan TODAS las entradas de funding (las que tienen "rate":
        # estimadas previas o reales del sync anterior); se conservan las de
        # auditoría (auto_open, import, manual_adjust…). Las reales no llevan
        # "kind" para que la UI/chart las traten como pagos normales.
        keep = [p for p in (pos.get("payments") or []) if "rate" not in p]
        cum = 0.0
        funding = []
        for inc in incomes:
            cum += inc["amount"]
            funding.append({"ts": int(inc["ts"] / 1000), "rate": inc["amount"] / exposure,
                            "earned": inc["amount"], "cumulative": cum,
                            "tranId": inc["id"], "real": True})
        with self.app.app_context():
            row = UserPosition.query.get(int(pos["id"]))
            if not row or row.status != "active":
                return
            row.earned_real = cum
            row.payment_count = len(funding)
            row.avg_rate = (sum(f["rate"] for f in funding) / len(funding)) if funding else 0
            row.last_fr_used = funding[-1]["rate"] if funding else (row.last_fr_used or 0)
            row.last_earn_update = time.time()
            row.payments_json = keep + funding
            row.earnings_source = "real"
            db.session.commit()

    def _drift_alerts(self, uid, drift: list):
        if not drift or not self.worker or not self.worker.email_notifier:
            return
        day = datetime.now().strftime("%Y%m%d")
        alerts = [{
            "type": "POSITION_DRIFT", "severity": "WARNING",
            "symbol": d.get("symbol", ""), "exchange": "Binance", "user_id": uid,
            "_exc_bucket": day,
            "dedup_key": f"POSITION_DRIFT:{uid}:{d.get('symbol')}:{d.get('kind')}:{day}",
            "message": d.get("msg", ""),
        } for d in drift]
        try:
            self.worker._dispatch_alerts_per_user(alerts)
        except Exception as e:
            log.warning(f"drift alert dispatch: {e}")

    # ── Wallet BSC ─────────────────────────────────────────────────────
    def _sync_wallet(self, uid: int):
        from core.db_persistence import DBPersistence
        with self.app.app_context():
            from core.db_models import UserWallet
            w = UserWallet.query.filter_by(user_id=uid, chain="bsc").first()
            if not w:
                return
            address = w.address
            positions = [p for p in DBPersistence().load_user_state(uid)["positions"]
                         if p.get("spot_venue") == "bsc"]
        try:
            from exchanges import onchain_bsc
            data = onchain_bsc.wallet_snapshot(address, [p["symbol"] for p in positions])
            ok, err = True, None
        except Exception as e:
            data, ok, err = {}, False, f"{type(e).__name__}: {e}"
            log.warning(f"bsc wallet snapshot user={uid}: {e}")
        with self.app.app_context():
            DBPersistence().save_account_snapshot(uid, "bsc_wallet", data, ok=ok, error=err)
