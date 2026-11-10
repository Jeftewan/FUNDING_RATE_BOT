#!/usr/bin/env python3
"""Liquida en basyo una posición spot_perp que los SEGUROS ya cerraron en el exchange.

Caso ARIA #56 (2026-11-10): el SL recompró el short y la venta límite Kyber vendió el spot,
pero el cierre del vigilante falló (token Alpha offline) y la posición quedó `active`.
Este script lee los fills reales y la cierra por la misma ruta que un cierre normal
(`JobRunner._finalize_close`: user_history con price_pnl, exec_json y notas):
  * SL (algo order) → actualOrderId → `trade_executor.order_fills` (precio y comisión).
  * Venta límite: Kyber (`kyber_limit_order.get_order` + `_bsc_filled`) o Binance spot.
  * Funding real: FUNDING_FEE desde la entrada (`BinanceAccount.funding_income`).
  * price_pnl = (venta spot − spot_entry·qty) + (perp_entry·qty − costo recompra).
Aborta si el short sigue abierto o si las piernas no se llenaron completas.
Solo LEE el exchange (no envía órdenes). DRY-RUN por defecto; `--apply` escribe la DB.
FERNET_KEY (para descifrar las keys del usuario) se toma de Railway con --railway; nunca se imprime.

    python scripts/settle_position.py --pos 56 --railway [--apply]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RAILWAY_PROJECT = "7dc7767e-983c-45a6-afc0-be5210dee632"
RAILWAY_VARS = ("FERNET_KEY", "EXCHANGE_PROXY_URL")


def _load_env():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _railway_env():
    out = subprocess.run(["railway", "variables", "--json", "-p", RAILWAY_PROJECT, "-e", "production",
                          "-s", "FUNDING_RATE_BOT"], capture_output=True, text=True, shell=os.name == "nt")
    try:
        data = json.loads(out.stdout)
    except ValueError:
        sys.exit("No se pudieron leer las variables de Railway (¿railway login?)")
    for k in RAILWAY_VARS:
        if data.get(k):
            os.environ[k] = data[k]


def _app():
    from flask import Flask
    from core.database import db
    url = os.environ["DATABASE_URL"]
    for pre in ("postgres://", "postgresql://"):
        if url.startswith(pre):
            url = "postgresql+psycopg2://" + url[len(pre):]
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=url, SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    return app


class _Worker:
    _notified_alerts = {}


def _ts(sec):
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def settle(app, pos_id):
    """→ (pos, res, info) con los fills reales; aborta si no está liquidada en el exchange."""
    from core.db_models import UserPosition, UserWallet
    from core.db_persistence import DBPersistence
    from exchanges import trade_executor as te
    from exchanges.binance_account import BinanceAccount
    from execution.jobs import position_token

    with app.app_context():
        row = UserPosition.query.get(pos_id)
        if not row or row.status != "active":
            sys.exit(f"posición {pos_id} no existe o no está activa")
        dbp = DBPersistence()
        pos = dbp._pos_to_dict(row)
        uid = row.user_id
        creds = dbp.load_user_exchange_keys(uid, row.exchange)
        wallet = UserWallet.query.filter_by(user_id=uid, chain="bsc").first()
        address = wallet.address if wallet else None
        token = position_token(pos) if pos.get("spot_venue") == "bsc" else None
    if not creds or str(creds.get("api_key", "")).startswith("gAAAA"):
        sys.exit("No se pudieron descifrar las API keys (¿FERNET_KEY? usa --railway)")
    prot = pos.get("protection") or {}
    sym = pos["symbol"]
    perp_sym = te.to_ccxt_symbol(sym, "swap")
    perp = te._build_client(pos["exchange"], creds, default_type="swap")
    perp.load_time_difference()
    te._ensure_markets(perp)

    short = te._open_perp_size(perp, perp_sym, "short")
    if short is None or short > 0:
        sys.exit(f"El short sigue abierto o no se pudo leer (short={short}): no está liquidada")

    # Pierna perp: el SL.
    sl = prot.get("perp") or {}
    o = perp.fetch_order(sl["id"], perp_sym, {"trigger": True})
    if te._algo_state(o) != "filled" or not (o.get("info") or {}).get("actualOrderId"):
        sys.exit(f"El SL {sl.get('id')} no figura llenado (estado {te._algo_state(o)})")
    actual = o["info"]["actualOrderId"]
    pf = te.order_fills(perp, perp_sym, actual)
    trades = perp.fetch_my_trades(perp_sym, params={"orderId": actual})
    sl_ts = min(t["timestamp"] for t in trades) / 1000 if trades else None

    # Pierna spot: la venta límite del seguro.
    spot = prot.get("spot") or {}
    if spot.get("venue") == "bsc":
        from exchanges import kyber_limit_order as klo
        ko = klo.get_order(address, spot["id"])
        sold, usdt = te._bsc_filled(spot, ko)
        txs = (ko or {}).get("transactions") or []
        spot_ts = max(int(t.get("txTime") or 0) for t in txs) if txs else None
        spot_ids = [str(spot["id"])] + [t.get("txHash") for t in txs]
        spot_fee = 0.0
        from exchanges import onchain_bsc as ob
        left = ob.from_wei(ob.erc20_balance(token["address"], address), int(token["decimals"])) if token else None
    else:
        spot_cli = te._build_client(pos["exchange"], creds, default_type="spot")
        spot_cli.load_time_difference()
        te._ensure_markets(spot_cli)
        spot_sym = te.to_ccxt_symbol(sym, "spot")
        f = te.order_fills(spot_cli, spot_sym, spot["id"])
        sold, usdt, spot_fee = f["gross"], f["cost"], f["fee_usd"]
        spot_ts, spot_ids, left = None, [str(spot["id"])], None

    spot_qty, perp_qty = float(pos["spot_qty"]), float(pos["perp_qty"])
    if pf["gross"] < perp_qty * 0.99 or sold < spot_qty * 0.99:
        sys.exit(f"Llenados incompletos: perp {pf['gross']:g}/{perp_qty:g}, spot {sold:g}/{spot_qty:g}")

    # Funding real del exchange desde la entrada.
    entry_ms = int(pos.get("entry_time") or 0)
    income = BinanceAccount(creds).funding_income(sym, entry_ms)
    funding = round(sum(r["amount"] for r in income), 8)

    spot_entry = float(pos.get("spot_entry_price") or 0)
    perp_entry = float(pos.get("entry_price") or 0)
    spot_leg = usdt - spot_entry * sold
    perp_leg = perp_entry * pf["gross"] - pf["cost"]
    spot_px, perp_px = usdt / sold, pf["cost"] / pf["gross"]
    res = {
        "price_pnl": round(spot_leg + perp_leg, 6),
        "exit_fees_usd": round(pf["fee_usd"] + spot_fee, 6),
        "protection_filled": {"perp_filled": pf["gross"], "perp_cost": pf["cost"],
                              "spot_filled": sold, "spot_cost": usdt},
        "order_ids": [str(actual)] + spot_ids[:1],
        "spot_exit_price": spot_px, "perp_exit_price": perp_px,
        "exec": {"spot_exit_price": spot_px, "perp_exit_price": perp_px,
                 "exit_basis_pct": round((spot_px / perp_px - 1) * 100, 4),
                 "spot_leg_pnl": round(spot_leg, 6), "perp_leg_pnl": round(perp_leg, 6),
                 "sl_trigger": prot.get("sl_price"), "sl_fill_ts": sl_ts, "spot_fill_ts": spot_ts,
                 "spot_fills": len(spot_ids) - 1, "spot_dust_left": left,
                 "settled_by": "scripts/settle_position.py"},
    }
    info = {"uid": uid, "funding_exchange": funding, "funding_db": float(pos.get("earned_real") or 0),
            "n_funding": len(income), "sl_ts": sl_ts, "spot_ts": spot_ts, "pf": pf, "sold": sold,
            "usdt": usdt, "spot_leg": spot_leg, "perp_leg": perp_leg, "left": left}
    return pos, res, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="escribe en la DB (por defecto dry-run)")
    ap.add_argument("--railway", action="store_true", help="toma FERNET_KEY de Railway (no se imprime)")
    args = ap.parse_args()
    _load_env()
    if args.railway:
        _railway_env()
    app = _app()
    pos, res, info = settle(app, args.pos)

    entry_fees = float(pos.get("entry_fees_real") if pos.get("entry_fees_real") is not None
                       else pos.get("entry_fees") or 0)
    fees = entry_fees + res["exit_fees_usd"]
    funding = info["funding_exchange"]
    net = funding + res["price_pnl"] - fees
    end = info["sl_ts"] or time.time()
    hours = (end - float(pos.get("entry_time") or 0) / 1000) / 3600
    pf = info["pf"]
    print(f"Posición #{args.pos} {pos['symbol']} (user {info['uid']}), {hours:.1f} h")
    print(f"  SL llenado {_ts(info['sl_ts']) if info['sl_ts'] else '?'}: {pf['gross']:g} @ "
          f"{res['perp_exit_price']:.6g} (trigger {res['exec']['sl_trigger']}), comisión {pf['fee_usd']:.4f}")
    print(f"  Venta spot {_ts(info['spot_ts']) if info['spot_ts'] else '?'}: {info['sold']:g} → "
          f"{info['usdt']:.4f} USDT @ {res['spot_exit_price']:.6g} ({res['exec']['spot_fills']} llenados), "
          f"polvo {info['left']}")
    print(f"  Funding: exchange {funding:+.4f} ({info['n_funding']} pagos) | DB {info['funding_db']:+.4f}")
    print(f"  Precio: spot {info['spot_leg']:+.4f} perp {info['perp_leg']:+.4f} → {res['price_pnl']:+.4f} "
          f"(basis de salida {res['exec']['exit_basis_pct']:+.3f} %)")
    print(f"  Fees: entrada {entry_fees:.4f} + salida {res['exit_fees_usd']:.4f} = {fees:.4f}")
    print(f"  NETO: {net:+.4f} USD")
    if not args.apply:
        print("DRY-RUN: nada escrito (usa --apply)")
        return

    from core.database import db
    from core.db_models import UserHistory, UserPosition
    from execution.jobs import JobRunner
    with app.app_context():
        row = UserPosition.query.get(args.pos)
        if abs(funding - (row.earned_real or 0)) > 1e-6:
            row.earned_real = funding
            db.session.commit()
    JobRunner(app, _Worker())._finalize_close(info["uid"], pos, res["exit_fees_usd"], res, "settle")
    with app.app_context():
        h = (UserHistory.query.filter_by(user_id=info["uid"], symbol=pos["symbol"])
             .order_by(UserHistory.id.desc()).first())
        p = UserPosition.query.get(args.pos)
        h.reason = p.close_reason = "protection_sl"
        h.hours = round(hours, 4)
        if info["sl_ts"]:
            h.closed_at = p.closed_at = datetime.fromtimestamp(info["sl_ts"], timezone.utc)
        h.notes = (h.notes or "") + (f"Liquidada con scripts/settle_position.py: SL y venta límite del seguro "
                                     f"ya ejecutados en el exchange @ {datetime.now().isoformat()}\n")
        db.session.commit()
        print(f"Escrito: user_history #{h.id} net {h.net_earned:+.4f} price_pnl {h.price_pnl:+.4f} "
              f"fees {h.fees:.4f}; posición {p.status}")


if __name__ == "__main__":
    main()
