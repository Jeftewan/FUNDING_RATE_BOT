"""Backfill del PnL neto REAL en posiciones auto ya cerradas/abiertas.

Antes del cambio, user_history.net_earned = funding − fees: no incluía el PnL de
precio de las dos piernas (basis de entrada/salida, slippage, comisión del DEX).
Este script lo reconstruye desde los resultados guardados en execution_jobs:

  - user_history (price_pnl NULL, notas "Auto-cierre job #N"):
      price_pnl = (spot_exit − spot_entry)·spot_sold + (perp_entry − perp_exit)·perp_closed
      net_earned = earned + price_pnl − fees
  - user_positions activas auto sin spot_entry_price -> lo toma del job de apertura.

Por defecto es DRY-RUN (solo imprime). `--apply` escribe en la DB.

    python scripts/backfill_price_pnl.py [--user 2] [--apply]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

from sqlalchemy import create_engine, text


def _engine():
    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except ImportError:
        pass
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("ERROR: DATABASE_URL no está configurada.")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(url, pool_pre_ping=True)


def _has_col(c, table, col) -> bool:
    """Las columnas nuevas las crea el deploy (_run_migrations); antes de eso el
    dry-run igual funciona y --apply se rechaza."""
    try:
        return bool(c.execute(text("select 1 from information_schema.columns where table_name=:t "
                                   "and column_name=:c"), {"t": table, "c": col}).first())
    except Exception:
        return True   # SQLite/otros: asumir esquema actual


def _job(c, jid):
    r = c.execute(text("select id, kind, position_id, result_json from execution_jobs where id=:i"),
                  {"i": jid}).first()
    return dict(r._mapping) if r else None


def _spot_entry(open_res: dict):
    if not open_res:
        return None
    if open_res.get("spot_avg_price_gross"):
        return float(open_res["spot_avg_price_gross"])
    for leg in open_res.get("legs") or []:
        if leg.get("kind") == "spot" and leg.get("price"):
            return float(leg["price"])
    return open_res.get("spot_avg_price")


def history_rows(c, user):
    q = ("select id, user_id, symbol, earned, fees, net_earned, notes from user_history "
         "where notes like '%Auto-cierre job #%'")
    if _has_col(c, "user_history", "price_pnl"):
        q += " and price_pnl is null"
    params = {}
    if user:
        q += " and user_id=:u"
        params["u"] = user
    out = []
    for h in c.execute(text(q + " order by id"), params):
        h = dict(h._mapping)
        m = re.findall(r"Auto-cierre job #(\d+)", h["notes"] or "")
        close = _job(c, int(m[-1])) if m else None
        if not close or not close.get("position_id"):
            out.append({**h, "skip": "sin job de cierre"})
            continue
        res = close["result_json"] or {}
        sep = "spot_entry_price" if _has_col(c, "user_positions", "spot_entry_price") else "null as spot_entry_price"
        pos = c.execute(text(f"select id, job_id, entry_price, {sep}, entry_basis_pct "
                             "from user_positions where id=:p"), {"p": close["position_id"]}).first()
        pos = dict(pos._mapping) if pos else {}
        open_job = _job(c, pos.get("job_id")) if pos.get("job_id") else None
        open_res = (open_job or {}).get("result_json") or {}
        spot_entry = pos.get("spot_entry_price") or _spot_entry(open_res)
        perp_entry = pos.get("entry_price") or open_res.get("entry_price")
        se, pe = res.get("spot_exit_price"), res.get("perp_exit_price")
        ss, pc = float(res.get("spot_sold") or 0), float(res.get("perp_closed") or 0)
        if not (spot_entry and perp_entry and se and pe):
            out.append({**h, "skip": "faltan precios de entrada/salida"})
            continue
        pnl = round((se - spot_entry) * ss + (perp_entry - pe) * pc, 6)
        new_net = (h["earned"] or 0) + pnl - (h["fees"] or 0)
        ex = {"spot_entry": spot_entry, "perp_entry": perp_entry, "spot_exit": se, "perp_exit": pe,
              "spot_qty_out": ss, "perp_qty_out": pc,
              "entry_basis_pct": pos.get("entry_basis_pct") or open_res.get("basis_pct"),
              "exit_basis_pct": round((pe - se) / se * 100, 4), "backfill": True}
        out.append({**h, "price_pnl": pnl, "new_net": new_net, "exec": ex,
                    "close_job": close["id"], "open_job": (open_job or {}).get("id")})
    return out


def position_rows(c, user):
    if not _has_col(c, "user_positions", "spot_entry_price"):
        q = ("select id, user_id, symbol, job_id from user_positions where status='active' "
             "and source='auto' and job_id is not null")
        params = {"u": user} if user else {}
        rows = c.execute(text(q + (" and user_id=:u" if user else "")), params)
        return [{**dict(p._mapping), "spot_entry_price": _spot_entry((_job(c, p.job_id) or {}).get("result_json") or {})}
                for p in rows]
    q = ("select id, user_id, symbol, job_id from user_positions where status='active' "
         "and source='auto' and spot_entry_price is null and job_id is not null")
    params = {}
    if user:
        q += " and user_id=:u"
        params["u"] = user
    out = []
    for p in c.execute(text(q), params):
        p = dict(p._mapping)
        oj = _job(c, p["job_id"])
        se = _spot_entry((oj or {}).get("result_json") or {})
        if se:
            out.append({**p, "spot_entry_price": se})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", type=int, help="limitar a un user_id")
    ap.add_argument("--apply", action="store_true", help="escribir en la DB (por defecto dry-run)")
    a = ap.parse_args()
    eng = _engine()
    with eng.connect() as c:
        schema_ok = _has_col(c, "user_history", "price_pnl") and _has_col(c, "user_positions", "spot_entry_price")
        hist = history_rows(c, a.user)
        pos = [p for p in position_rows(c, a.user) if p.get("spot_entry_price")]
    if not schema_ok:
        print("(Columnas nuevas aún no migradas en esta DB: despliega primero; --apply bloqueado.)")

    print(f"== Historial ({len(hist)} filas sin price_pnl)")
    for h in hist:
        if h.get("skip"):
            print(f"  #{h['id']} {h['symbol']}: SKIP — {h['skip']}")
            continue
        e = h["exec"]
        print(f"  #{h['id']} {h['symbol']} (jobs abrir #{h['open_job']} / cerrar #{h['close_job']})")
        print(f"     spot {e['spot_entry']:.8g} -> {e['spot_exit']:.8g} x{e['spot_qty_out']:g} | "
              f"perp {e['perp_entry']:.8g} -> {e['perp_exit']:.8g} x{e['perp_qty_out']:g}")
        print(f"     basis entrada {e['entry_basis_pct']}% salida {e['exit_basis_pct']}%")
        print(f"     funding {h['earned']:+.4f}  precio {h['price_pnl']:+.4f}  fees -{h['fees']:.4f}  "
              f"| neto {h['net_earned']:+.4f} -> {h['new_net']:+.4f}")
    print(f"== Posiciones activas sin spot_entry_price ({len(pos)})")
    for p in pos:
        print(f"  #{p['id']} {p['symbol']}: spot_entry_price = {p['spot_entry_price']:.8g} (job #{p['job_id']})")

    if not a.apply or not schema_ok:
        print("\nDRY-RUN: no se escribió nada." + (" Usa --apply para guardar." if schema_ok else ""))
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    with eng.begin() as c:
        for h in hist:
            if h.get("skip"):
                continue
            c.execute(text("update user_history set price_pnl=:pp, net_earned=:n, exec_json=:ex, "
                           "notes=coalesce(notes,'') || :note where id=:i"),
                      {"pp": h["price_pnl"], "n": h["new_net"], "ex": json.dumps(h["exec"]), "i": h["id"],
                       "note": f"Backfill PnL de precio {h['price_pnl']:+.6f} @ {stamp}\n"})
        for p in pos:
            c.execute(text("update user_positions set spot_entry_price=:s where id=:i"),
                      {"s": p["spot_entry_price"], "i": p["id"]})
    print(f"\nAPLICADO: {sum(1 for h in hist if not h.get('skip'))} historial, {len(pos)} posiciones.")


if __name__ == "__main__":
    main()
