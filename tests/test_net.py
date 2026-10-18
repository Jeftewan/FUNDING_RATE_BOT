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


BINANCE_451 = ('binance GET https://api.binance.com/api/v3/exchangeInfo 451  {"code": 0, "msg": '
               '"Service unavailable from a restricted location according to \'b. Eligibility\'"}')
BYBIT_403 = ("bybit GET https://api.bybit.com/v5/market/time 403 Forbidden "
             "error:The Amazon CloudFront distribution is configured to block access from your country")


def test_geoblock_detection():
    assert net.is_geoblock_error(BINANCE_451)
    assert net.is_geoblock_error(Exception(BYBIT_403))
    assert not net.is_geoblock_error("binance 429 Too Many Requests")


def test_geoblocked_ttl(monkeypatch):
    net._geoblocked.clear()
    net.mark_geoblocked("Binance")
    assert net.geoblocked() == ["binance"]
    monkeypatch.setattr(net.time, "time", lambda: 10**12)
    assert net.geoblocked() == []


def test_egress_status_hides_proxy(monkeypatch):
    monkeypatch.setenv("EXCHANGE_PROXY_URL", "socks5h://u:secreto@1.2.3.4:1080")
    s = net.egress_status()
    assert s["proxy"] is True
    assert "1.2.3.4" not in str(s) and "secreto" not in str(s)


def test_log_egress_blocked_country(monkeypatch, caplog):
    import requests

    class R:
        def json(self):
            return {"country": "US", "ip": "152.55.188.35"}
    monkeypatch.delenv("EXCHANGE_PROXY_URL", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    with caplog.at_level("INFO", logger="bot"):
        net.log_egress()
    assert net.egress_status()["country"] == "US"
    assert any(r.levelname == "ERROR" and "451/403" in r.getMessage() for r in caplog.records)


def test_manager_marks_geoblock():
    from exchanges.manager import ExchangeManager
    net._geoblocked.clear()
    m = ExchangeManager.__new__(ExchangeManager)
    m._exchanges, m._rate_cache, m._last_fetch_ts, m._ban_until = {"binance": None}, {}, {}, {}

    def boom(name):
        raise Exception(BINANCE_451)
    m._fetch_exchange_rates = boom
    assert m.fetch_all_funding_rates() == {"binance": []}
    assert net.geoblocked() == ["binance"]
    assert m._ban_until == {}  # geobloqueo ≠ ban por rate limit


def test_account_sync_geoblock_alert_dedup():
    from execution.account_sync import AccountSyncer

    sent = []

    class W:
        email_notifier = object()

        def _dispatch_alerts_per_user(self, alerts):
            sent.extend(alerts)
    s = AccountSyncer(app=None, scanner_worker=W())
    s._geoblock_alert(2, "Binance")
    s._geoblock_alert(2, "Binance")
    assert sent[0]["type"] == "EXCHANGE_GEOBLOCKED" and sent[0]["user_id"] == 2
    assert sent[0]["dedup_key"] == sent[1]["dedup_key"]  # el notifier deduplica por día
