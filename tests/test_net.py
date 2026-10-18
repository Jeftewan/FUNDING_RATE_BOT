"""Proxy de salida hacia exchanges (EXCHANGE_PROXY_URL)."""
from exchanges import net
from exchanges import trade_executor as te


def test_no_proxy(monkeypatch):
    monkeypatch.delenv("EXCHANGE_PROXY_URL", raising=False)
    assert net.ccxt_proxy_params() == {}
    assert net.requests_proxies() is None
    assert net.describe() == "direct"


def test_socks_proxy(monkeypatch):
    url = "socks5h://u:secreto@1.2.3.4:1080"
    monkeypatch.setenv("EXCHANGE_PROXY_URL", url)
    assert net.ccxt_proxy_params() == {"socksProxy": url}
    assert net.requests_proxies() == {"http": url, "https": url}
    assert net.describe() == "socks5h://1.2.3.4:1080"  # sin credenciales


def test_http_proxy(monkeypatch):
    url = "http://u:p@1.2.3.4:8080"
    monkeypatch.setenv("EXCHANGE_PROXY_URL", url)
    assert net.ccxt_proxy_params() == {"httpsProxy": url}


def test_user_client_uses_proxy(monkeypatch):
    url = "socks5h://u:p@1.2.3.4:1080"
    monkeypatch.setenv("EXCHANGE_PROXY_URL", url)
    c = te._build_client("binance", {"api_key": "k", "api_secret": "s"})
    assert c.socksProxy == url
