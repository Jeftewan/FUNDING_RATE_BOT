"""Cuenta Binance del usuario — sync de solo lectura (+ 2 acciones explícitas).

Lee con las keys DEL USUARIO: permisos de la key, modo de cuenta (Portfolio
Margin / Multi-Assets / hedge), saldos spot y USDⓈ-M, posiciones, income de
funding (FUNDING_FEE) real. Las únicas escrituras son acciones que el usuario
confirma en la UI: desactivar Multi-Assets y transferir USDT spot↔futuros.

Reusa los clientes de ``trade_executor`` (cache de mercados, demo trading con
TRADE_SANDBOX=1).
"""
import logging
import time

from exchanges.trade_executor import (
    _build_client, _ensure_markets, _sandbox, binance_max_leverage, to_ccxt_symbol,
)

log = logging.getLogger("bot.binance_acct")

# Dust: saldos spot por debajo de esto (USD) no cuentan como pierna spot.
DUST_USD = 5.0


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class BinanceAccount:
    def __init__(self, creds: dict):
        self.creds = creds
        self.spot = _build_client("binance", creds, default_type="spot")
        self.fut = _build_client("binance", creds, default_type="swap")
        self._markets_loaded = False

    def _markets(self):
        if not self._markets_loaded:
            _ensure_markets(self.spot)
            _ensure_markets(self.fut)
            self._markets_loaded = True

    # ── Permisos y modo ────────────────────────────────────────────────
    def permissions(self) -> dict:
        """/sapi/v1/account/apiRestrictions. {} en demo (sin sapi)."""
        if _sandbox():
            return {"demo": True}
        return self.spot.sapi_get_account_apirestrictions() or {}

    @staticmethod
    def evaluate_permissions(perm: dict) -> dict:
        """{ok, errors[], warnings[]} — reglas de seguridad de basyo."""
        if perm.get("demo"):
            return {"ok": True, "errors": [], "warnings": ["Modo demo: permisos no verificables"]}
        errors, warnings = [], []
        if perm.get("enableWithdrawals"):
            errors.append("La key tiene RETIROS habilitados. Por seguridad basyo no acepta keys con "
                          "retiros: crea una key sin ese permiso.")
        if not perm.get("enableReading"):
            errors.append("Falta el permiso de lectura (Enable Reading).")
        if not perm.get("enableSpotAndMarginTrading"):
            warnings.append("Sin permiso de trading Spot: no se podrá ejecutar la pierna spot.")
        if not perm.get("enableFutures"):
            warnings.append("Sin permiso de Futuros: no se podrá abrir el short ni leer posiciones.")
        if not perm.get("ipRestrict"):
            warnings.append("La key no está restringida por IP. Recomendado: restringirla a la IP "
                            "de salida de basyo.")
        if perm.get("enableInternalTransfer"):
            warnings.append("La key permite transferencias internas a otras cuentas; no es necesario.")
        return {"ok": not errors, "errors": errors, "warnings": warnings}

    def account_mode(self, perm: dict = None) -> dict:
        perm = perm if perm is not None else {}
        out = {"portfolio_margin": bool(perm.get("enablePortfolioMarginTrading")),
               "multi_assets": None, "hedge_mode": None}
        try:
            r = self.fut.fapiprivate_get_multiassetsmargin()
            out["multi_assets"] = str(r.get("multiAssetsMargin")).lower() == "true"
        except Exception as e:
            log.warning(f"multiAssetsMargin: {e}")
        try:
            r = self.fut.fapiprivate_get_positionside_dual()
            out["hedge_mode"] = str(r.get("dualSidePosition")).lower() == "true"
        except Exception as e:
            log.warning(f"positionSide/dual: {e}")
        return out

    # ── Saldos y posiciones ────────────────────────────────────────────
    def balances(self) -> dict:
        self._markets()
        spot = self.spot.fetch_balance()
        free = spot.get("free") or {}
        total = spot.get("total") or {}
        acct = self.fut.fapiprivatev3_get_account() or {}
        usdt_asset = next((a for a in acct.get("assets", []) if a.get("asset") == "USDT"), {})
        holdings = {k: _f(v) for k, v in total.items() if _f(v) > 0 and k != "USDT"}
        return {
            "spot_usdt_free": _f(free.get("USDT")),
            "spot_usdt_total": _f(total.get("USDT")),
            "spot_holdings": holdings,
            "spot_free": {k: _f(v) for k, v in free.items() if _f(v) > 0},
            "futures_wallet": _f(usdt_asset.get("walletBalance", acct.get("totalWalletBalance"))),
            "futures_available": _f(usdt_asset.get("availableBalance", acct.get("availableBalance"))),
            "futures_unrealized": _f(acct.get("totalUnrealizedProfit")),
            "futures_margin_balance": _f(acct.get("totalMarginBalance")),
        }

    def positions(self) -> list:
        """Posiciones USDⓈ-M abiertas (contracts ≠ 0), con liquidación real."""
        self._markets()
        out = []
        for p in self.fut.fetch_positions():
            contracts = _f(p.get("contracts"))
            if not contracts:
                continue
            sym = p.get("symbol") or ""
            if not sym.endswith(":USDT"):
                continue
            info = p.get("info") or {}
            out.append({
                "symbol": sym.split("/")[0],
                "ccxt_symbol": sym,
                "side": p.get("side"),
                "qty": abs(contracts) * _f(p.get("contractSize"), 1.0),
                "entry_price": _f(p.get("entryPrice")),
                "mark_price": _f(p.get("markPrice")),
                "liq_price": _f(p.get("liquidationPrice")) or None,
                "notional": abs(_f(p.get("notional"))),
                "leverage": _f(p.get("leverage") or info.get("leverage")) or None,
                "margin_mode": p.get("marginMode") or str(info.get("marginType", "")).lower(),
                "isolated_margin": _f(info.get("isolatedMargin") or p.get("initialMargin")),
                "unrealized_pnl": _f(p.get("unrealizedPnl")),
                "update_time": int(_f(info.get("updateTime")) or p.get("timestamp") or 0),
            })
        return out

    def funding_income(self, symbol: str, since_ms: int) -> list:
        """FUNDING_FEE del símbolo desde since_ms (paginado). [{id, ts, amount}]."""
        self._markets()
        sym = to_ccxt_symbol(symbol, "swap")
        out, seen, since = [], set(), int(since_ms)
        for _ in range(20):
            rows = self.fut.fetch_funding_history(sym, since=since, limit=1000)
            fresh = [r for r in rows if r.get("id") not in seen]
            for r in fresh:
                seen.add(r.get("id"))
                out.append({"id": str(r.get("id")), "ts": int(r.get("timestamp") or 0),
                            "amount": _f(r.get("amount"))})
            if len(rows) < 1000 or not fresh:
                break
            since = max(r["ts"] for r in out) + 1
        out.sort(key=lambda r: r["ts"])
        return out

    # ── Acciones explícitas (confirmadas por el usuario) ───────────────
    def disable_multi_assets(self) -> dict:
        if any(True for _ in self.positions()):
            return {"ok": False, "msg": "Hay posiciones de futuros abiertas: Binance no permite "
                                        "cambiar Multi-Assets con posiciones abiertas."}
        try:
            self.fut.fapiprivate_post_multiassetsmargin({"multiAssetsMargin": "false"})
            return {"ok": True, "msg": "Multi-Assets desactivado (Single-Asset mode)"}
        except Exception as e:
            return {"ok": False, "msg": f"No se pudo desactivar Multi-Assets: {e}"}

    def transfer_usdt(self, amount: float, to: str) -> dict:
        """to='future' (spot→USDⓈ-M) o 'spot'. Requiere 'Permits Universal Transfer'."""
        src, dst = ("spot", "future") if to == "future" else ("future", "spot")
        try:
            r = self.spot.transfer("USDT", float(amount), src, dst)
            return {"ok": True, "id": r.get("id")}
        except Exception as e:
            return {"ok": False, "msg": f"Transferencia {src}→{dst} falló: {e}"}

    # ── Agregados ──────────────────────────────────────────────────────
    def check(self) -> dict:
        """Prueba de conexión enriquecida (botón 'Probar conexión')."""
        try:
            perm = self.permissions()
        except Exception as e:
            return {"ok": False, "msg": f"Conexión fallida: {type(e).__name__}: {e}"}
        ev = self.evaluate_permissions(perm)
        try:
            mode = self.account_mode(perm)
            bal = self.balances()
        except Exception as e:
            return {"ok": False, "msg": f"Lectura de cuenta falló: {e}", "permissions": ev}
        msg = "Conexión OK" if ev["ok"] else "Key rechazada: " + " ".join(ev["errors"])
        return {"ok": ev["ok"], "msg": msg, "permissions": ev, "account_mode": mode,
                "usdt_balance": round(bal["spot_usdt_free"] + bal["futures_available"], 2),
                "balances": bal}

    def snapshot(self, basyo_positions: list) -> dict:
        """Estado completo para user_account_snapshots + importables + drift."""
        perm = self.permissions()
        ev = self.evaluate_permissions(perm)
        mode = self.account_mode(perm)
        bal = self.balances()
        pos = self.positions()
        self._markets()

        prices = {}
        def spot_px(base):
            if base not in prices:
                try:
                    t = self.spot.fetch_ticker(f"{base}/USDT")
                    prices[base] = _f(t.get("last"))
                except Exception:
                    prices[base] = 0.0
            return prices[base]

        tracked = {(p.get("symbol") or "").upper() for p in basyo_positions
                   if (p.get("exchange") or "").lower() == "binance"
                   and p.get("mode", "spot_perp") == "spot_perp"}
        importable = []
        for p in pos:
            if p["side"] != "short" or p["symbol"].upper() in tracked:
                continue
            held = bal["spot_holdings"].get(p["symbol"], 0.0)
            held_usd = held * (spot_px(p["symbol"]) or p["mark_price"])
            importable.append({**p, "spot_held": held if held_usd >= DUST_USD else 0.0,
                               "hedged_pct": round(min(1.0, held / p["qty"]) * 100, 1)
                               if p["qty"] and held_usd >= DUST_USD else 0.0})

        drift = []
        by_sym = {(p["symbol"].upper(), p["side"]): p for p in pos}
        for bp in basyo_positions:
            if (bp.get("exchange") or "").lower() != "binance" or bp.get("mode") != "spot_perp":
                continue
            if bp.get("source") not in ("auto", "imported"):
                continue
            real = by_sym.get(((bp.get("symbol") or "").upper(), "short"))
            want = _f(bp.get("perp_qty"))
            if not real:
                drift.append({"position_id": bp.get("id"), "symbol": bp.get("symbol"),
                              "kind": "missing", "msg": "El short ya no existe en Binance "
                                                        "(¿cerrado manualmente o liquidado?)"})
            elif want and abs(real["qty"] - want) / want > 0.05:
                drift.append({"position_id": bp.get("id"), "symbol": bp.get("symbol"),
                              "kind": "qty_mismatch", "real_qty": real["qty"], "basyo_qty": want,
                              "msg": f"Short real {real['qty']:g} vs registrado {want:g}"})
            held = bal["spot_holdings"].get((bp.get("symbol") or "").upper(), 0.0)
            want_spot = _f(bp.get("spot_qty"))
            if bp.get("spot_venue", "binance") in ("binance", "") and want_spot \
                    and held < want_spot * 0.95:
                drift.append({"position_id": bp.get("id"), "symbol": bp.get("symbol"),
                              "kind": "spot_missing", "real_qty": held, "basyo_qty": want_spot,
                              "msg": f"Spot en Binance {held:g} < registrado {want_spot:g}"})

        return {
            "permissions": ev, "account_mode": mode, "balances": bal,
            "positions": pos, "importable": importable, "drift": drift,
            "ts": time.time(),
        }

    def max_leverage(self, symbol: str):
        self._markets()
        return binance_max_leverage(self.fut, to_ccxt_symbol(symbol, "swap"))
