"""¿Los exchanges dan servicio desde esta salida? (diagnóstico de geobloqueo 451/403).

Uso:
    python scripts/egress_check.py                       # salida directa (p. ej. en el VPS)
    EXCHANGE_PROXY_URL=socks5h://u:p@ip:puerto python scripts/egress_check.py   # vía proxy

Solo endpoints públicos, sin keys. Criterio: todo 200, ningún 451/403.
"""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exchanges.net import describe, requests_proxies  # noqa: E402

ENDPOINTS = {
    "binance spot": "https://api.binance.com/api/v3/ping",
    "binance futuros": "https://fapi.binance.com/fapi/v1/ping",
    "binance sapi": "https://api.binance.com/sapi/v1/system/status",
    "binance alpha list": "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/"
                          "wallet/cex/alpha/all/token/list",
    "binance web3": "https://web3.binance.com/build/api/v1/dex/aggregator/quote",
    "bybit": "https://api.bybit.com/v5/market/time",
    "okx": "https://www.okx.com/api/v5/public/time",
    "bitget": "https://api.bitget.com/api/v2/public/time",
}


def main():
    px = requests_proxies()
    try:
        j = requests.get("https://ipinfo.io/json", proxies=px, timeout=10).json()
        print(f"Salida: {j.get('country')} {j.get('city')} {j.get('ip')} ({j.get('org')}) via {describe()}")
    except Exception as e:
        print(f"Salida: no se pudo consultar ipinfo via {describe()}: {e}")
    bad = 0
    for name, url in ENDPOINTS.items():
        try:
            r = requests.get(url, proxies=px, timeout=15)
            # web3 sin firma responde 4xx de auth, que NO es geobloqueo; 451/403 + texto de región sí.
            geo = r.status_code in (451, 403) and any(
                s in r.text.lower() for s in ("restricted location", "country", "region", "40301"))
            if name == "binance web3":
                geo = geo or '"code":40301' in r.text
            status = "BLOQUEADO" if geo else ("OK" if r.status_code < 400 or name == "binance web3" else "ERROR")
            bad += status != "OK"
            print(f"  {status:9} {r.status_code}  {name}")
        except Exception as e:
            bad += 1
            print(f"  FALLO      -   {name}: {type(e).__name__}: {str(e)[:100]}")
    print("Resultado:", "OK — todos responden" if not bad else f"{bad} con problemas")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
