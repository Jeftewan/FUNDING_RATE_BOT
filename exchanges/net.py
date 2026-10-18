"""Salida de red hacia exchanges por un proxy opcional (bloqueo geográfico).

EXCHANGE_PROXY_URL = socks5h://user:pass@host:port | http://user:pass@host:port
Vacío → salida directa. Solo lo usan los clientes ccxt (CEX), la Binance Web3 API
/ 0x y la token list Alpha de binance.com. Postgres, Telegram, Gemini, DeFi y el
RPC BSC salen directo (no dependen del proxy).
"""
import logging
import os
from urllib.parse import urlsplit

log = logging.getLogger("bot")

EGRESS_URL = "https://ipinfo.io/json"


def exchange_proxy() -> str:
    return os.environ.get("EXCHANGE_PROXY_URL", "").strip()


def ccxt_proxy_params() -> dict:
    """Params para el constructor ccxt (socksProxy / httpsProxy)."""
    url = exchange_proxy()
    if not url:
        return {}
    return {"socksProxy": url} if urlsplit(url).scheme.startswith("socks") else {"httpsProxy": url}


def requests_proxies():
    """`proxies=` para requests, o None (directo)."""
    url = exchange_proxy()
    return {"http": url, "https": url} if url else None


def describe() -> str:
    """Ruta de salida sin credenciales (apta para logs)."""
    url = exchange_proxy()
    if not url:
        return "direct"
    p = urlsplit(url)
    return f"{p.scheme}://{p.hostname}:{p.port}"


def log_egress():
    """Loguea país/IP de salida hacia exchanges (diagnóstico de bloqueos 451/403)."""
    import requests
    try:
        j = requests.get(EGRESS_URL, proxies=requests_proxies(), timeout=10).json()
        log.info(f"Egress exchanges: {j.get('country')} {j.get('ip')} via {describe()}")
    except Exception as e:
        log.warning(f"Egress exchanges: check falló via {describe()}: {e}")
