"""Corrige el PnL de precio de un cierre cuyo short se recompró FUERA de basyo.

Caso JCT (user_history.id=46, 2026-10-24): la venta límite del spot se llenó
(+438.44 USD) y el usuario recompró el short a mano en la web de Binance; el
cierre (job #16, "Short real 0 < registrado") no contó esa recompra → price_pnl
de una sola pierna. Este script busca la recompra real (userTrades del perp desde
la apertura, las compras más recientes hasta cubrir el short faltante, con
`trade_executor.external_perp_buyback`, la misma función del cierre) y recalcula:

    price_pnl  += perp_entry·qty − costo_recompra
    fees       += comisión de la recompra
    net_earned  = earned + price_pnl − fees

Lee la DB con DATABASE_URL y Binance con BINANCE_USER_API_KEY/SECRET (.env, solo
lectura). Por defecto es DRY-RUN; `--apply` escribe SOLO esa fila de user_history.

    python scripts/fix_external_buyback.py --hist 46 [--apply]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from scripts.backfill_price_pnl import _engine, _job  # noqa: E402


def _load_env():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist", type=int, required=True, help="user_history.id a corregir")
    ap.add_argument("--apply", action="store_true", help="escribe en la DB (por defecto dry-run)")
    args = ap.parse_args()
    _load_env()
    key, secret = os.environ.get("BINANCE_USER_API_KEY"), os.environ.get("BINANCE_USER_API_SECRET")
    if not key or not secret:
        sys.exit("Define BINANCE_USER_API_KEY y BINANCE_USER_API_SECRET (en .env)")

    eng = _engine()
    with eng.connect() as c:
        h = c.execute(text("select * from user_history where id=:i"), {"i": args.hist}).first()
        if not h:
            sys.exit(f"user_history.id={args.hist} no existe")
        h = dict(h._mapping)
        ex = h.get("exec_json") or {}
        if isinstance(ex, str):
            ex = json.loads(ex)
        if ex.get("perp_external"):
            sys.exit("Ya corregido (exec_json.perp_external presente).")
        m = re.findall(r"Auto-cierre job #(\d+)", h.get("notes") or "")
        if not m:
            sys.exit("Sin 'Auto-cierre job #N' en las notas: no se puede ubicar el cierre.")
        job = _job(c, int(m[-1]))
        res = job.get("result_json") or {}
        if isinstance(res, str):
            res = json.loads(res)
        pos = c.execute(text("select id, symbol, entry_time, entry_price, perp_qty from user_positions "
                             "where id=:i"), {"i": job["position_id"]}).first()
        if not pos:
            sys.exit(f"Posición {job['position_id']} del job #{job['id']} no existe")
        pos = dict(pos._mapping)

    missing = float(pos["perp_qty"] or 0) - float(res.get("perp_closed") or 0)
    print(f"user_history #{h['id']} {h['symbol']}  job #{job['id']}  posición #{pos['id']}")
    print(f"  drift del cierre: {res.get('drift')}")
    print(f"  short sin recompra registrada: {missing:g}  (perp_entry {pos['entry_price']:.8g})")
    if missing <= 0:
        sys.exit("No falta recompra del short: nada que corregir.")

    from exchanges import trade_executor as te
    cli = te._build_client("binance", {"api_key": key, "api_secret": secret}, "swap")
    te._ensure_markets(cli)
    sym = te.to_ccxt_symbol(pos["symbol"], "swap")
    f = te.external_perp_buyback(cli, sym, missing, pos["entry_time"])
    if not f or f["gross"] < missing * 0.99:
        sys.exit(f"Recompra no encontrada en Binance ({(f or {}).get('gross') or 0:g} de {missing:g}); "
                 f"no se corrige.")
    perp_leg = float(pos["entry_price"]) * f["gross"] - f["cost"]
    old_pp = float(h["price_pnl"] or 0)
    new_pp = round(old_pp + perp_leg, 6)
    new_fees = round(float(h["fees"] or 0) + f["fee_usd"], 6)
    new_net = round(float(h["earned"] or 0) + new_pp - new_fees, 6)
    print(f"  recompra: {f['gross']:g} @ {f['avg']:.8g} = {f['cost']:.4f} USDT, comisión {f['fee_usd']:.4f}"
          f"  trades {len(f['trades'])}")
    print(f"  pierna perp: {perp_leg:+.4f}")
    print(f"  price_pnl  {old_pp:+.4f} -> {new_pp:+.4f}")
    print(f"  fees       {float(h['fees'] or 0):.4f} -> {new_fees:.4f}")
    print(f"  net_earned {float(h['net_earned'] or 0):+.4f} -> {new_net:+.4f}")

    if not args.apply:
        print("\nDRY-RUN: nada escrito. Repite con --apply para guardar.")
        return
    ex.update({"perp_external": {"missing": missing, **{k: f[k] for k in ("gross", "cost", "avg", "fee_usd")},
                                 "trades": f["trades"]},
               "perp_exit": f["avg"], "perp_qty_out": float(ex.get("perp_qty_out") or 0) + f["gross"]})
    note = (f"Corrección {datetime.now().isoformat()}: recompra del short fuera de basyo "
            f"{f['gross']:g}@{f['avg']:.8g} (perp {perp_leg:+.4f}); price_pnl {old_pp:+.4f}->{new_pp:+.4f}\n")
    with eng.begin() as c:
        c.execute(text("update user_history set price_pnl=:pp, fees=:fe, net_earned=:ne, exec_json=:ej, "
                       "notes=coalesce(notes, '') || :no where id=:i"),
                  {"pp": new_pp, "fe": new_fees, "ne": new_net, "ej": json.dumps(ex, default=str),
                   "no": note, "i": h["id"]})
    print("\nAPLICADO.")


if __name__ == "__main__":
    main()
