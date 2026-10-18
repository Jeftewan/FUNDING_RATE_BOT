"""Salida de red hacia exchanges por un proxy opcional (bloqueo geográfico).

EXCHANGE_PROXY_URL = socks5h://user:pass@host:port | http://user:pass@host:port
Vacío → salida directa. Solo lo usan los clientes ccxt (CEX), la Binance Web3 API
/ 0x y la token list Alpha de binance.com. Postgres, Telegram, Gemini, DeFi y el
RPC BSC salen directo (no dependen del proxy).
"""
import logging
import os
import re
import time
from urllib.parse import urlsplit

log = logging.getLogger("bot")

EGRESS_URL = "https://ipinfo.io/json"

# Países desde los que Binance/Bybit niegan servicio. Las IPs propias de Railway
# (AS400940, 152.55.x) geolocalizan en San Francisco sin importar la región.
BLOCKED_COUNTRIES = {"US"}

# 451 de Binance ("restricted location") y 403 CloudFront de Bybit ("block access
# from your country").
_GEOBLOCK_RE = re.compile(r"restricted location|block access from your country", re.I)
GEOBLOCK_TTL = 3600

_egress = {}       # {country, proxy} del último log_egress
_geoblocked = {}   # {exchange: ts del último 451/403 geográfico}


def is_geoblock_error(err) -> bool:
    return bool(_GEOBLOCK_RE.search(str(err)))


def mark_geoblocked(exchange: str):
    _geoblocked[exchange.lower()] = time.time()


def geoblocked() -> list:
    """Exchanges con geobloqueo visto en la última hora."""
    now = time.time()
    return sorted(ex for ex, ts in _geoblocked.items() if now - ts < GEOBLOCK_TTL)


def egress_status() -> dict:
    """Resumen para /health (público): sin IP ni host del proxy."""
    return {"country": _egress.get("country"), "proxy": bool(exchange_proxy()),
            "geoblocked": geoblocked()}


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
        country = j.get("country")
        _egress.update(country=country)
        log.info(f"Egress exchanges: {country} {j.get('ip')} via {describe()}")
        if country in BLOCKED_COUNTRIES:
            log.error(f"Egress exchanges en {country}: Binance/Bybit responderán 451/403. "
                      "Configurar EXCHANGE_PROXY_URL (VPS en país permitido) o una IP "
                      "estática no-US.")
    except Exception as e:
        log.warning(f"Egress exchanges: check falló via {describe()}: {e}")
