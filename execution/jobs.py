"""Jobs de ejecución real en background (open/close, TWAP pareado).

Por qué: la ejecución (ventana maker, TWAP de horas) no cabe en un request
(gunicorn --timeout 120). El request crea un ExecutionJob y devuelve job_id;
un thread por job ejecuta vía trade_executor y persiste progreso/resultado.

Recuperación: al arrancar, los jobs que quedaron 'running' (redeploy de
Railway mata los threads) se RECONCILIAN contra el exchange por clientOrderId
(bsy{job}-…): se re-cubre el perp hasta igualar el spot en manos de la
posición y se guarda/actualiza la posición con lo realmente ejecutado.
Nunca se reanuda un TWAP a ciegas: el usuario decide lanzar el resto.

Límite: 1 job activo por usuario para las acciones del usuario (submit_exclusive,
atómico frente al doble clic). El vigilante de seguros puede lanzar un cierre
aunque haya otro job: la pierna descubierta no puede esperar a un TWAP ajeno.
"""
import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("bot.jobs")

_ACTIVE = ("queued", "running")


class JobRunner:
    def __init__(self, app, scanner_worker, syncer=None):
        self.app = app
        self.worker = scanner_worker
        self.syncer = syncer
        self._started = False
        # Gunicorn corre 1 worker: un lock de proceso basta para que el chequeo
        # "¿hay job activo?" y la creación del job sean atómicos.
        self._submit_lock = threading.Lock()

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._recover, daemon=True, name="jobs-recover").start()
        from execution.protection import ProtectionWatcher
        ProtectionWatcher(self).start()

    # ── API ────────────────────────────────────────────────────────────
    def active_job(self, user_id):
        from core.db_models import ExecutionJob
        return ExecutionJob.query.filter(ExecutionJob.user_id == user_id,
                                         ExecutionJob.status.in_(_ACTIVE)).first()

    def active_job_for_position(self, user_id, position_id):
        from core.db_models import ExecutionJob
        if position_id is None:
            return None
        return ExecutionJob.query.filter(ExecutionJob.user_id == user_id,
                                         ExecutionJob.position_id == int(position_id),
                                         ExecutionJob.status.in_(_ACTIVE)).first()

    def submit_exclusive(self, user_id: int, kind: str, params: dict):
        """submit() solo si el usuario no tiene otro job activo (atómico). None si ocupado."""
        with self._submit_lock:
            if self.active_job(user_id):
                return None
            return self.submit(user_id, kind, params)

    def submit(self, user_id: int, kind: str, params: dict) -> int:
        """Crea el job (llamar dentro de app context) y lanza su thread."""
        from core.database import db
        from core.db_models import ExecutionJob
        job = ExecutionJob(user_id=user_id, kind=kind, status="queued",
                           params_json=params, progress_json={}, result_json={},
                           position_id=params.get("position_id"))
        db.session.add(job)
        db.session.commit()
        jid = job.id
        threading.Thread(target=self._run, args=(jid,), daemon=True, name=f"job-{jid}").start()
        return jid

    @staticmethod
    def to_dict(job) -> dict:
        return {
            "id": job.id, "kind": job.kind, "status": job.status,
            "params": job.params_json or {}, "progress": job.progress_json or {},
            "result": job.result_json or {}, "error": job.error,
            "position_id": job.position_id, "cancel_requested": bool(job.cancel_requested),
            "created_at": job.created_at.timestamp() if job.created_at else None,
            "updated_at": job.updated_at.timestamp() if job.updated_at else None,
        }

    # ── Persistencia ───────────────────────────────────────────────────
    def _update(self, jid, **fields):
        from core.database import db
        from core.db_models import ExecutionJob
        with self.app.app_context():
            job = ExecutionJob.query.get(jid)
            if not job:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = datetime.now(timezone.utc)
            db.session.commit()

    def _cancelled(self, jid) -> bool:
        from core.db_models import ExecutionJob
        with self.app.app_context():
            job = ExecutionJob.query.get(jid)
            return bool(job and job.cancel_requested)

    def _load(self, jid):
        from core.db_models import ExecutionJob
        with self.app.app_context():
            job = ExecutionJob.query.get(jid)
            if not job:
                return None
            return {"id": job.id, "user_id": job.user_id, "kind": job.kind,
                    "params": dict(job.params_json or {}), "status": job.status,
                    "created_at": job.created_at, "position_id": job.position_id}

    def _alert(self, uid, symbol, msg, severity="CRITICAL", typ="EXEC_FAILURE"):
        if not self.worker or not self.worker.email_notifier:
            return
        ts = int(time.time())
        try:
            self.worker._dispatch_alerts_per_user([{
                "type": typ, "severity": severity, "symbol": symbol, "exchange": "",
                "user_id": uid, "funding_ts": ts,
                "dedup_key": f"{typ}_{symbol}_{uid}_{ts}", "message": msg,
            }])
        except Exception as e:
            log.warning(f"job alert failed: {e}")

    # ── Ejecución ──────────────────────────────────────────────────────
    def _ctx(self, jid):
        from exchanges.trade_executor import ExecContext
        last = {"t": 0}

        def progress(data):
            # Throttle de escrituras a DB (≥1 s entre updates).
            if time.time() - last["t"] >= 1:
                last["t"] = time.time()
                self._update(jid, progress_json=data)

        return ExecContext(job_id=jid, progress=progress, cancelled=lambda: self._cancelled(jid))

    def _creds(self, uid, exchanges):
        from core.db_persistence import DBPersistence
        out = {}
        with self.app.app_context():
            dbp = DBPersistence()
            for ex in exchanges:
                c = dbp.load_user_exchange_keys(uid, ex)
                if not c:
                    raise RuntimeError(f"Sin API keys para {ex}")
                out[ex.lower()] = c
        return out

    def _load_wallet(self, uid):
        from core.db_persistence import DBPersistence
        with self.app.app_context():
            return DBPersistence().load_user_wallet(uid, "bsc")

    def _spot_leg_factory(self, uid, params):
        if params.get("spot_venue") != "bsc":
            return None
        from exchanges import onchain_bsc
        wallet = self._load_wallet(uid)
        if not wallet:
            raise RuntimeError("Configura tu wallet BSC dedicada en la pestaña Cuenta")
        token = params.get("token") or {}
        return lambda perp_cli, perp_sym, ctx: onchain_bsc.BscSpotLeg(
            wallet, token, ctx, perp_cli, perp_sym,
            max_slippage=float(params.get("max_slippage", onchain_bsc.DEFAULT_SLIPPAGE)))

    def _run(self, jid):
        job = self._load(jid)
        if not job:
            return
        self._update(jid, status="running")
        try:
            if job["kind"] == "open":
                self._run_open(job)
            else:
                self._run_close(job)
        except Exception as e:
            log.exception(f"job {jid} crashed: {e}")
            self._update(jid, status="failed", error=f"{type(e).__name__}: {e}")
            self._alert(job["user_id"], job["params"].get("symbol", ""),
                        f"Job de ejecución #{jid} falló: {e}. Revisa tu cuenta en el exchange.")
        finally:
            if self.syncer:
                self.syncer.request()

    def _run_open(self, job):
        from exchanges import trade_executor as texec
        jid, uid, p = job["id"], job["user_id"], job["params"]
        opp = p["opportunity"]
        mode = opp.get("mode", "spot_perp")
        needed = [opp.get("exchange", "")] if mode == "spot_perp" \
            else [opp.get("long_exchange", ""), opp.get("short_exchange", "")]
        creds = self._creds(uid, needed)
        ctx = self._ctx(jid)
        res = texec.execute_open(creds, opp, float(p["capital"]), int(p["leverage"]),
                                 dry_run=False,
                                 allow_market_fallback=bool(p.get("allow_market_fallback", True)),
                                 ctx=ctx, twap=p.get("twap") or None,
                                 spot_leg_factory=self._spot_leg_factory(uid, p))
        fill = res if res.get("ok") else res.get("partial")
        pos_id = None
        if fill and (fill.get("perp_qty") or 0) > 0:
            # entry_time = inicio del job: el funding cobrado durante un TWAP largo
            # también pertenece a esta posición.
            started = job.get("created_at")
            entry_ms = int(started.replace(tzinfo=timezone.utc).timestamp() * 1000) if started else None
            pos_id = self._save_open_position(uid, jid, opp, p, fill, entry_ms=entry_ms)
        prot = None
        # También una apertura parcial (falló la cobertura del resto) queda con seguros.
        if pos_id and mode == "spot_perp":
            prot = self._protect(uid, pos_id, creds, ctx, token=p.get("token"))
            res["protection"] = prot
        status = "done" if res.get("ok") else "failed"
        self._update(jid, status=status, result_json=_jsonable(res), position_id=pos_id,
                     error=None if res.get("ok") else res.get("msg"))
        sym = opp.get("symbol", "")
        if res.get("ok"):
            sl_txt = f". Seguros: SL {prot['sl_price']:g}" if prot and prot.get("ok") else ""
            self._alert(uid, sym, f"Posición abierta: spot {res.get('spot_qty') or 0:g} / short "
                        f"{res.get('perp_qty') or 0:g}. Fees ${res.get('entry_fees_usd', 0):.2f}"
                        + sl_txt
                        + (f". {res['partial_note']}" if res.get("partial_note") else ""),
                        severity="INFO", typ="EXEC_DONE")
        elif "unwound" in res or res.get("code") == "HEDGE_FAILED":
            self._alert(uid, sym, f"Fallo al abrir {sym} (auto): {res.get('msg', '')}")

    # ── Seguros (SL del short + venta límite del spot) ─────────────────
    def _save_protection(self, pos_id, prot):
        from core.database import db
        from core.db_models import UserPosition
        with self.app.app_context():
            row = UserPosition.query.get(int(pos_id))
            if row:
                row.protection_json = _jsonable(prot)
                db.session.commit()

    def _load_pos(self, pos_id):
        from core.db_models import UserPosition
        from core.db_persistence import DBPersistence
        with self.app.app_context():
            row = UserPosition.query.get(int(pos_id))
            return DBPersistence()._pos_to_dict(row) if row and row.status == "active" else None

    def _bsc_args(self, uid, pos, token=None):
        if pos.get("spot_venue") != "bsc":
            return None, None
        from exchanges import onchain_bsc
        return self._load_wallet(uid), (token or onchain_bsc.alpha_token(pos.get("symbol", "")))

    def _protect(self, uid, pos_id, creds, ctx=None, token=None):
        """Coloca los seguros de una posición spot_perp recién abierta y los
        persiste. Un fallo nunca deshace la posición: alerta para ponerlos a mano."""
        from exchanges import trade_executor as texec
        pos = self._load_pos(pos_id)
        if not pos:
            return None
        sym = pos.get("symbol", "")
        try:
            wallet, token = self._bsc_args(uid, pos, token)
            prot = texec.place_protection(creds, pos, ctx=ctx, wallet=wallet, token=token)
        except Exception as e:
            log.exception(f"place_protection pos={pos_id}: {e}")
            prot = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        if not prot.get("ok"):
            prot = {**prot, "status": "failed"}
            self._alert(uid, sym, f"Seguros NO colocados en {sym}: {prot.get('msg')}. "
                                  f"Pon el SL del short y la venta del spot manualmente.")
        elif prot.get("errors"):
            self._alert(uid, sym, f"Seguros incompletos en {sym} (SL {prot['sl_price']:g}): "
                                  + "; ".join(prot["errors"]))
        self._save_protection(pos_id, prot)
        return prot

    def _restore_protection(self, uid, pos, creds, token=None):
        """Tras un cierre fallido/parcial: si los seguros se cancelaron, se vuelven
        a poner sobre lo que queda abierto."""
        from exchanges import trade_executor as texec
        prot = pos.get("protection") or {}
        if prot.get("status") != "active" or (pos.get("exchange") or "").lower()                 not in texec.PROTECTION_EXCHANGES:
            return
        try:
            wallet, _ = self._bsc_args(uid, pos, token)
            st = texec.protection_status(creds, pos, wallet)
            if "open" in (st["perp"], st["spot"]):
                return
        except Exception as e:
            log.warning(f"protection status pos={pos.get('id')}: {e}")
        self._protect(uid, pos["id"], creds, token=token)

    def _save_open_position(self, uid, jid, opp, p, fill, entry_ms=None) -> int:
        from core.db_persistence import DBPersistence
        from portfolio.manager import open_position
        # Estimaciones (fees est., steps) del camino bookkeeping, sin validar
        # capital: ya se validó al crear el job y las órdenes ya se ejecutaron.
        ok, result = open_position({"total_capital": 1e15, "max_positions": 10 ** 9,
                                    "positions": []}, opp, float(p["capital"]), int(p["leverage"]))
        pos = result["position"] if ok else {
            "symbol": opp.get("symbol"), "exchange": opp.get("exchange", ""),
            "mode": opp.get("mode", "spot_perp"), "entry_time": int(time.time() * 1000),
            "ih": opp.get("interval_hours", 8),
            "entry_fr": opp.get("funding_rate", opp.get("rate_differential", 0)),
        }
        is_binance = (opp.get("exchange") or "").lower() == "binance" and pos.get("mode") == "spot_perp"
        if entry_ms:
            pos["entry_time"] = entry_ms
        pos.update({
            "entry_price": fill.get("entry_price") or pos.get("entry_price"),
            "entry_fees_real": fill.get("entry_fees_usd"),
            "capital_used": round(fill.get("capital_used") or float(p["capital"]), 4),
            "exposure": round(fill.get("exposure") or pos.get("exposure", 0), 4),
            "leverage": int(p["leverage"]),
            "auto_executed": True, "source": "auto", "job_id": jid,
            "spot_qty": fill.get("spot_qty"), "perp_qty": fill.get("perp_qty"),
            "spot_venue": fill.get("spot_venue", ""),
            "entry_basis_pct": fill.get("basis_pct"),
            "spot_entry_price": fill.get("spot_avg_price_gross") or fill.get("spot_avg_price"),
            "entry_slippage_usd": (fill.get("entry_slippage") or {}).get("total"),
            "entry_mode": fill.get("entry_mode", ""),
            "earnings_source": "real" if is_binance else "estimated",
            "payments_json": [{
                "ts": time.time(), "kind": "auto_open", "job_id": jid,
                "order_ids": fill.get("order_ids", []), "legs": fill.get("legs", []),
                "note": "Auto-ejecutado" + (f" — {fill['partial_note']}" if fill.get("partial_note") else ""),
            }],
        })
        try:
            with self.app.app_context():
                return DBPersistence().save_position(uid, pos)
        except Exception as e:
            log.exception(f"save position after fills FAILED job={jid}: {e}")
            self._alert(uid, opp.get("symbol", ""),
                        f"Órdenes ejecutadas pero NO se pudo guardar la posición (job #{jid}): {e}. "
                        f"Impórtala desde Cuenta → Binance.")
            return None

    def _run_close(self, job):
        from core.db_models import UserPosition
        from core.db_persistence import DBPersistence
        from exchanges import trade_executor as texec
        jid, uid, p = job["id"], job["user_id"], job["params"]
        with self.app.app_context():
            row = UserPosition.query.filter_by(id=int(p["position_id"]), user_id=uid,
                                               status="active").first()
            if not row:
                self._update(jid, status="failed", error="Posición no encontrada")
                return
            pos = DBPersistence()._pos_to_dict(row)
        mode = pos.get("mode", "spot_perp")
        needed = [pos.get("exchange", "")] if mode == "spot_perp" \
            else [pos.get("long_exchange", ""), pos.get("short_exchange", "")]
        creds = self._creds(uid, needed)
        res = texec.execute_close(creds, pos, dry_run=False, ctx=self._ctx(jid),
                                  twap=p.get("twap") or None,
                                  spot_leg_factory=self._spot_leg_factory(
                                      uid, {**p, "spot_venue": pos.get("spot_venue"),
                                            "token": p.get("token") or {}}))
        sym = pos.get("symbol", "")
        if not res.get("ok"):
            self._update(jid, status="failed", result_json=_jsonable(res), error=res.get("msg"))
            self._alert(uid, sym, f"Fallo al cerrar {sym} (auto): {res.get('msg', '')}")
            # Si algo se ejecutó, ajustar cantidades registradas (y sus fees reales).
            if res.get("spot_sold") or res.get("perp_closed"):
                self._apply_partial_close(pos, res, add_fees=float(res.get("exit_fees_usd") or 0))
            self._restore_protection(uid, self._load_pos(pos["id"]) or pos, creds, p.get("token"))
            return

        exit_fees = float(res.get("exit_fees_usd") or 0)
        if not res.get("spot_sold") and not res.get("perp_closed") and \
                (res.get("partial_note") or not res.get("complete", True)):
            # Nada se ejecutó: es un fallo, no un cierre parcial (antes quedaba 'done' sin alerta).
            msg = res.get("partial_note") or "No se ejecutó ninguna orden"
            self._update(jid, status="failed", result_json=_jsonable(res), error=msg)
            self._alert(uid, sym, f"Fallo al cerrar {sym} (auto): {msg}. La posición sigue abierta.")
            self._restore_protection(uid, pos, creds, p.get("token"))
            return
        if res.get("complete", True) and not res.get("partial_note"):
            # Último income real antes de cerrar (captura el pago más reciente).
            self._final_income(uid, pos)
            self._finalize_close(uid, pos, exit_fees, res, jid)
            pp = res.get("price_pnl")
            self._alert(uid, sym, f"Posición cerrada. Fees salida ${exit_fees:.2f}"
                        + (f". PnL de precio (basis+slippage) ${pp:+.2f}" if pp is not None else ""),
                        severity="INFO", typ="EXEC_DONE")
            self._update(jid, status="done", result_json=_jsonable(res))
        else:
            self._apply_partial_close(pos, res, add_fees=exit_fees)
            self._update(jid, status="done", result_json=_jsonable(res),
                         error=res.get("partial_note"))
            remaining = self._load_pos(pos["id"])
            if remaining:
                self._restore_protection(uid, remaining, creds, p.get("token"))

    def _apply_partial_close(self, pos, res, add_fees=0.0):
        from core.database import db
        from core.db_models import UserPosition
        with self.app.app_context():
            row = UserPosition.query.get(int(pos["id"]))
            if not row:
                return
            if row.spot_qty is not None:
                row.spot_qty = max(0.0, (row.spot_qty or 0) - float(res.get("spot_sold") or 0))
            if row.perp_qty is not None:
                row.perp_qty = max(0.0, (row.perp_qty or 0) - float(res.get("perp_closed") or 0))
            if add_fees:
                row.exit_fees_real = (row.exit_fees_real or 0) + add_fees
            if res.get("price_pnl") is not None:
                row.realized_price_pnl = (row.realized_price_pnl or 0) + float(res["price_pnl"])
            pj = list(row.payments_json or [])
            pj.append({"ts": time.time(), "kind": "auto_close_partial",
                       "order_ids": res.get("order_ids", []), "legs": res.get("legs", []),
                       "price_pnl": res.get("price_pnl"), "exec": res.get("exec"),
                       "note": res.get("partial_note") or res.get("msg", "")})
            row.payments_json = pj
            db.session.commit()

    def _final_income(self, uid, pos):
        if not self.syncer or (pos.get("exchange") or "").lower() != "binance" \
                or pos.get("source") not in ("auto", "imported"):
            return
        try:
            from exchanges.binance_account import BinanceAccount
            creds = self._creds(uid, [pos["exchange"]])[pos["exchange"].lower()]
            self.syncer.update_real_earnings(BinanceAccount(creds), pos)
        except Exception as e:
            log.warning(f"final income sync failed: {e}")

    def _finalize_close(self, uid, pos, exit_fees, res, jid):
        from core.database import db
        from core.db_models import UserPosition, UserHistory
        from core.db_persistence import DBPersistence
        from portfolio.manager import position_fees
        with self.app.app_context():
            row = UserPosition.query.get(int(pos["id"]))
            if not row or row.status != "active":
                return
            symbol = row.symbol
            row.exit_fees_real = (row.exit_fees_real or 0) + exit_fees
            if row.protection_json:
                row.protection_json = {**row.protection_json, "status": "closed",
                                       "closed_at": time.time()}
            db.session.commit()
            el_h = (time.time() - (row.entry_time or 0) / 1000) / 3600
            _e, _x, fees, _ = position_fees({
                "entry_fees": row.entry_fees or 0, "exit_fees_est": row.exit_fees_est or 0,
                "entry_fees_real": row.entry_fees_real, "exit_fees_real": row.exit_fees_real,
            })
            earned = row.earned_real or 0
            pnl = real_net_breakdown(row, res)
            DBPersistence().close_position(row.id, {
                "reason": "auto", "hours": el_h, "fees": fees,
                "net_earned": earned + (pnl["price_pnl"] or 0) - fees, **pnl,
            })
            hist = (UserHistory.query.filter_by(user_id=uid, symbol=symbol)
                    .order_by(UserHistory.id.desc()).first())
            if hist:
                ids = ", ".join(str(i) for i in res.get("order_ids", []))
                px = ""
                if res.get("spot_exit_price") and res.get("perp_exit_price"):
                    px = f" spot@{res['spot_exit_price']:.6g} perp@{res['perp_exit_price']:.6g}"
                hist.notes = (hist.notes or "") + (
                    f"Auto-cierre job #{jid} orders=[{ids}]{px} @ {datetime.now().isoformat()}\n")
                db.session.commit()
        for k in [k for k in list(self.worker._notified_alerts) if symbol in k]:
            self.worker._notified_alerts.pop(k, None)

    # ── Recuperación tras reinicio ─────────────────────────────────────
    def _recover(self):
        time.sleep(10)
        from core.db_models import ExecutionJob
        try:
            with self.app.app_context():
                stale = [(j.id, j.status) for j in
                         ExecutionJob.query.filter(ExecutionJob.status.in_(_ACTIVE)).all()]
        except Exception as e:
            log.warning(f"job recovery query failed: {e}")
            return
        for jid, status in stale:
            if status == "queued":
                threading.Thread(target=self._run, args=(jid,), daemon=True).start()
                continue
            try:
                self._reconcile(jid)
            except Exception as e:
                log.exception(f"reconcile job {jid} failed: {e}")
                self._update(jid, status="interrupted",
                             error=f"Interrumpido y NO reconciliado: {e} — revisa el exchange")
                job = self._load(jid)
                if job:
                    self._alert(job["user_id"], job["params"].get("symbol", ""),
                                f"Job #{jid} interrumpido por reinicio y no se pudo reconciliar: "
                                f"{e}. Revisa tu cuenta.")

    def _reconcile(self, jid):
        from core.database import db
        from core.db_models import UserPosition
        from core.db_persistence import DBPersistence
        from exchanges import trade_executor as texec
        job = self._load(jid)
        uid, p = job["user_id"], job["params"]
        if job["kind"] == "open":
            opp = p["opportunity"]
            symbol, exchange = opp.get("symbol", ""), opp.get("exchange", "")
            pos = None
        else:
            with self.app.app_context():
                row = UserPosition.query.get(int(p["position_id"]))
                pos = DBPersistence()._pos_to_dict(row) if row else {}
            symbol, exchange = pos.get("symbol", ""), pos.get("exchange", "")
            opp = None
        mode = (opp or pos or {}).get("mode", "spot_perp")
        venue = p.get("spot_venue") or (pos or {}).get("spot_venue")
        if mode != "spot_perp" or venue == "bsc":
            raise RuntimeError("reconciliación automática solo para spot_perp en CEX")
        creds = self._creds(uid, [exchange])[exchange.lower()]
        since_ms = int(job["created_at"].replace(tzinfo=timezone.utc).timestamp() * 1000) - 60_000 \
            if job["created_at"] else int((time.time() - 86400) * 1000)
        rec = texec.reconcile_spot_perp(creds, exchange, symbol, f"bsy{jid}-", since_ms,
                                        job["kind"], pos)
        pos_id = None
        if job["kind"] == "open":
            if rec["perp_qty"] > 0:
                mark = texec._mid_price(texec._build_client(exchange, creds, "swap"),
                                        texec.to_ccxt_symbol(symbol, "swap"), opp.get("price", 0))
                pos_id = self._save_open_position(uid, jid, opp, p, {
                    "spot_qty": rec["spot_qty"], "perp_qty": rec["perp_qty"],
                    "entry_price": mark, "entry_fees_usd": rec["fees_usd"],
                    "exposure": rec["perp_qty"] * mark, "spot_venue": exchange.lower(),
                    "capital_used": rec["spot_qty"] * mark + rec["perp_qty"] * mark / max(1, int(p["leverage"])),
                    "entry_mode": "reconciled", "partial_note": rec["msg"],
                })
        else:
            with self.app.app_context():
                row = UserPosition.query.get(int(p["position_id"]))
                if row:
                    row.spot_qty, row.perp_qty = rec["spot_qty"], rec["perp_qty"]
                    db.session.commit()
            if rec["perp_qty"] <= 0 and rec["spot_qty"] <= 0:
                self._finalize_close(uid, pos, rec["fees_usd"], {"order_ids": []}, jid)
        self._update(jid, status="interrupted", position_id=pos_id or job.get("position_id"),
                     result_json={"reconciled": rec}, error=f"Interrumpido por reinicio. {rec['msg']}")
        self._alert(uid, symbol, f"Job #{jid} interrumpido por reinicio. {rec['msg']}",
                    severity="WARNING")


def real_net_breakdown(row, res) -> dict:
    """PnL de precio total (parciales + este cierre), slippage entrada+salida y el
    detalle de ejecución para user_history. Neto real = funding + price_pnl − fees.

    price_pnl None (falta precio de entrada) → el neto queda funding − fees como antes.
    """
    partial = getattr(row, "realized_price_pnl", None)
    this = res.get("price_pnl")
    price_pnl = None if this is None and partial is None else round((partial or 0) + (this or 0), 6)
    exit_slip = (res.get("exit_slippage") or {}).get("total")
    entry_slip = getattr(row, "entry_slippage_usd", None)
    slip = None if exit_slip is None and entry_slip is None else round((entry_slip or 0) + (exit_slip or 0), 6)
    ex = dict(res.get("exec") or {})
    ex.update({"entry_basis_pct": getattr(row, "entry_basis_pct", None),
               "entry_slippage_usd": entry_slip, "partial_price_pnl": partial})
    return {"price_pnl": price_pnl, "slippage_usd": slip, "exec": _jsonable(ex)}


def _jsonable(obj):
    """Copia JSON-safe (floats/None/str) del resultado del executor."""
    import json
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return {"repr": str(obj)}
