"""Chequeo SOLO LECTURA de una cuenta Binance con las keys del usuario.

Imprime permisos de la key (con las reglas de basyo), modo de cuenta
(Portfolio Margin / Multi-Assets / hedge), saldos, posiciones USDⓈ-M e income
FUNDING_FEE de los últimos N días. No coloca órdenes ni cambia ajustes.

Uso (keys en .env o en el entorno, NUNCA en el código ni en el chat):
    BINANCE_USER_API_KEY=... BINANCE_USER_API_SECRET=... python scripts/binance_sync_check.py [--days 7]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    _load_env()
    key = os.environ.get("BINANCE_USER_API_KEY")
    secret = os.environ.get("BINANCE_USER_API_SECRET")
    if not key or not secret:
        sys.exit("Define BINANCE_USER_API_KEY y BINANCE_USER_API_SECRET (en .env)")

    from exchanges.binance_account import BinanceAccount
    acct = BinanceAccount({"api_key": key, "api_secret": secret})

    perm = acct.permissions()
    ev = acct.evaluate_permissions(perm)
    print("== Permisos ==")
    print(json.dumps({k: v for k, v in perm.items() if k.startswith(("enable", "ip", "permits"))}, indent=1))
    print("Veredicto basyo:", "OK" if ev["ok"] else "RECHAZADA", ev)

    print("\n== Modo de cuenta ==")
    print(acct.account_mode(perm))

    print("\n== Saldos ==")
    bal = acct.balances()
    print(json.dumps({k: v for k, v in bal.items() if k not in ("spot_free",)}, indent=1, default=str))

    print("\n== Posiciones USDⓈ-M ==")
    pos = acct.positions()
    for p in pos:
        print(f"  {p['symbol']:<10} {p['side']:<5} qty={p['qty']:g} entry={p['entry_price']:g} "
              f"mark={p['mark_price']:g} liq={p['liq_price']} x{p['leverage']} {p['margin_mode']}")
    if not pos:
        print("  (sin posiciones)")

    since = int((time.time() - args.days * 86400) * 1000)
    print(f"\n== Funding real últimos {args.days} días ==")
    for p in pos:
        inc = acct.funding_income(p["symbol"], since)
        print(f"  {p['symbol']}: {len(inc)} pagos, total ${sum(i['amount'] for i in inc):.4f}")


if __name__ == "__main__":
    main()
