"""DeFi perpetual exchange manager — fetches funding rates from DeFi protocols.

Supported: Hyperliquid, GMX (Arbitrum), Aster, Lighter, Extended.
All use public REST APIs (no auth required for read-only data).
"""
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.models import FundingRate

log = logging.getLogger("bot")

# Cache: avoid hammering DeFi APIs
MIN_FETCH_INTERVAL = 120  # seconds between fetches per exchange


def _calc_next_hourly_ts() -> int:
    """Calculate the next hour-boundary timestamp in ms.

    Used for DeFi protocols with hourly funding that don't expose
    a next-payment timestamp in their API.
    """
    now = time.time()
    next_hour = (int(now / 3600) + 1) * 3600
    return int(next_hour * 1000)


class DefiExchangeManager:
    """Fetches funding rates from DeFi perpetual exchanges."""

    EXCHANGES = ["Hyperliquid", "GMX", "Aster", "Lighter", "Extended"]

    def __init__(self, config=None):
        self.config = config
        self._last_fetch_ts = {}
        self._rate_cache = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "FundingRateBot/1.0"})

    def fetch_all_funding_rates(self) -> dict:
        """Fetch rates from all DeFi exchanges in parallel."""
        results = {}
        now = time.time()

        fetchers = {
            "Hyperliquid": self._fetch_hyperliquid,
            "GMX": self._fetch_gmx,
            "Aster": self._fetch_aster,
            "Lighter": self._fetch_lighter,
            "Extended": self._fetch_extended,
        }

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {}
            for name, fn in fetchers.items():
                last = self._last_fetch_ts.get(name, 0)
                if now - last < MIN_FETCH_INTERVAL and name in self._rate_cache:
                    results[name] = self._rate_cache[name]
                    continue
                futures[pool.submit(fn)] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    rates = future.result()
                    results[name] = rates
                    self._rate_cache[name] = rates
                    self._last_fetch_ts[name] = now
                    log.info(f"DeFi {name}: {len(rates)} pairs")
                except Exception as e:
                    log.error(f"DeFi {name} fetch error: {e}")
                    if name in self._rate_cache:
                        results[name] = self._rate_cache[name]
                    else:
                        results[name] = []

        return results

    # ── Hyperliquid ──────────────────────────────────────────────

    def _fetch_hyperliquid(self) -> list:
        """Hyperliquid: POST /info with metaAndAssetCtxs.
        Funding is hourly. Values are strings.
        """
        url = "https://api.hyperliquid.xyz/info"
        resp = self._session.post(url, json={"type": "metaAndAssetCtxs"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        meta = data[0]  # {universe: [{name, szDecimals, ...}, ...]}
        ctxs = data[1]  # [{funding, markPx, oraclePx, dayNtlVlm, ...}, ...]

        rates = []
        for asset_def, ctx in zip(meta["universe"], ctxs):
            symbol = asset_def["name"]
            try:
                fr = float(ctx.get("funding", "0"))
                price = float(ctx.get("markPx", "0"))
                vol24h = float(ctx.get("dayNtlVlm", "0"))
            except (ValueError, TypeError):
                continue

            if price <= 0:
                continue

            rates.append(FundingRate(
                symbol=symbol,
                pair=f"{symbol}USDT",
                exchange="Hyperliquid",
                rate=fr,
                price=price,
                volume_24h=vol24h,
                interval_hours=1,
                payments_per_day=24,
                next_funding_ts=_calc_next_hourly_ts(),
            ))

        return rates

    # ── GMX (Arbitrum) ───────────────────────────────────────────

    def _fetch_gmx(self) -> list:
        """GMX V2: GET /markets/info from Arbitrum API.
        Funding is continuous per-second. Convert to hourly equivalent.
        fundingFactorPerSecond is 30-decimal fixed-point.
        """
        url = "https://arbitrum-api.gmxinfra.io/markets/info"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        rates = []
        markets = data if isinstance(data, list) else data.get("markets", data.get("data", []))

        if isinstance(data, dict):
            # GMX returns {marketAddress: {market data}}
            items = data.items() if not isinstance(data.get("markets"), list) else []
            if items:
                for addr, info in items:
                    fr_obj = self._parse_gmx_market(info)
                    if fr_obj:
                        rates.append(fr_obj)
                return rates

        # Fallback: list format
        for market in (markets if isinstance(markets, list) else []):
            fr_obj = self._parse_gmx_market(market)
            if fr_obj:
                rates.append(fr_obj)

        return rates

    def _parse_gmx_market(self, info: dict) -> FundingRate:
        """Parse a single GMX market into a FundingRate."""
        try:
            # Extract symbol from various possible formats
            symbol = ""
            index_token = info.get("indexTokenSymbol", info.get("indexToken", {
            }))
            if isinstance(index_token, str):
                symbol = index_token.replace("WETH", "ETH").replace(
                    "WBTC", "BTC")
            if not symbol:
                market_name = info.get("name", info.get("marketToken", ""))
                if "/" in str(market_name):
                    symbol = str(market_name).split("/")[0].strip()

            if not symbol or symbol in ("", "USDC", "USDT", "DAI"):
                return None

            # Funding factor per second (30-decimal)
            ff_raw = info.get("fundingFactorPerSecond", "0")
            ff = float(ff_raw) / 1e30 if float(ff_raw) > 1e10 else float(ff_raw)

            # Convert per-second to hourly rate (to compare with 1h exchanges)
            hourly_rate = ff * 3600
            longs_pay = info.get("longsPayShorts", True)
            # Convention: positive = shorts earn
            if not longs_pay:
                hourly_rate = -hourly_rate

            price = 0
            for pkey in ("indexTokenPrice", "markPrice", "price"):
                if pkey in info:
                    pval = info[pkey]
                    if isinstance(pval, dict):
                        price = float(pval.get("max", pval.get("min", 0)))
                    else:
                        price = float(pval)
                    break
            if price > 1e20:
                price = price / 1e30

            if price <= 0 or abs(hourly_rate) < 1e-10:
                return None

            return FundingRate(
                symbol=symbol,
                pair=f"{symbol}USD",
                exchange="GMX",
                rate=hourly_rate,
                price=price,
                volume_24h=0,  # GMX doesn't expose 24h vol in this endpoint
                interval_hours=1,
                payments_per_day=24,
                next_funding_ts=_calc_next_hourly_ts(),
            )
        except Exception as e:
            log.debug(f"GMX market parse error: {e}")
            return None

    # ── Aster ────────────────────────────────────────────────────

    def _fetch_aster(self) -> list:
        """Aster: Binance-like API. GET /fapi/v1/premiumIndex.
        8-hour funding interval.
        """
        url = "https://fapi.asterdex.com/fapi/v1/premiumIndex"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        rates = []
        for item in data:
            try:
                pair = item.get("symbol", "")
                if not pair.endswith("USDT"):
                    continue
                symbol = pair.replace("USDT", "")
                fr = float(item.get("lastFundingRate", "0"))
                price = float(item.get("markPrice", "0"))
                next_ts = int(item.get("nextFundingTime", 0))
                # Aster occasionally returns 0 or a stale past value — fall
                # back to the next hour boundary so the snapshot unique
                # constraint can advance.  Same helper already used by
                # Hyperliquid/GMX/Lighter/Extended adapters above.
                now_ms = int(time.time() * 1000)
                if next_ts <= 0 or next_ts <= now_ms:
                    next_ts = _calc_next_hourly_ts()

                if price <= 0:
                    continue

                rates.append(FundingRate(
                    symbol=symbol,
                    pair=pair,
                    exchange="Aster",
                    rate=fr,
                    price=price,
                    volume_24h=0,
                    interval_hours=8,
                    payments_per_day=3,
                    next_funding_ts=next_ts,
                ))
            except (ValueError, TypeError):
                continue

        return rates

    # ── Lighter ──────────────────────────────────────────────────

    def _fetch_lighter(self) -> list:
        """Lighter: GET /api/v1/funding-rates.
        1-hour funding. Rates clamped [-0.5%, +0.5%]/hour.
        """
        url = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        rates = []
        items = data if isinstance(data, list) else data.get("data", data.get("fundingRates", []))

        for item in items:
            try:
                symbol = item.get("symbol", item.get("market", ""))
                # Clean symbol
                for suffix in ("USDT", "USD", "-PERP", "_PERP", "-USD"):
                    if symbol.upper().endswith(suffix):
                        symbol = symbol[:len(symbol) - len(suffix)]
                symbol = symbol.upper()

                if not symbol:
                    continue

                fr = float(item.get("fundingRate", item.get("rate", "0")))
                price = float(item.get("markPrice", item.get("price", "0")))

                if price <= 0:
                    continue

                rates.append(FundingRate(
                    symbol=symbol,
                    pair=f"{symbol}USD",
                    exchange="Lighter",
                    rate=fr,
                    price=price,
                    volume_24h=0,
                    interval_hours=1,
                    payments_per_day=24,
                    next_funding_ts=_calc_next_hourly_ts(),
                ))
            except (ValueError, TypeError):
                continue

        return rates

    # ── Extended ─────────────────────────────────────────────────

    def _fetch_extended(self) -> list:
        """Extended (Starknet): GET /api/v1/info/{market}/funding.
        1-hour funding. Multiple markets.
        First get markets list, then funding for each.
        """
        base = "https://api.starknet.extended.exchange/api/v1"
        rates = []

        # Try to get a market list or known markets
        known_markets = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD",
                         "ARB-USD", "AVAX-USD", "LINK-USD", "OP-USD",
                         "MATIC-USD", "WIF-USD", "PEPE-USD", "SUI-USD"]

        for market in known_markets:
            try:
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - 3600_000  # last hour
                url = f"{base}/info/{market}/funding?startTime={start_ms}&endTime={now_ms}"
                resp = self._session.get(url, timeout=10)
                if resp.status_code != 200:
                    continue
                data = resp.json()

                items = data if isinstance(data, list) else data.get("data", [])
                if not items:
                    continue

                # Get the most recent funding entry
                latest = items[0] if items else None
                if not latest:
                    continue

                fr = float(latest.get("f", latest.get("fundingRate", "0")))
                symbol = market.split("-")[0]

                # We need price — try to get from a ticker endpoint
                price = 0
                try:
                    ticker_url = f"{base}/info/{market}/ticker"
                    tr = self._session.get(ticker_url, timeout=5)
                    if tr.status_code == 200:
                        td = tr.json()
                        price = float(td.get("lastPrice", td.get("markPrice",
                                      td.get("p", "0"))))
                except Exception:
                    pass

                if price <= 0:
                    continue

                rates.append(FundingRate(
                    symbol=symbol,
                    pair=f"{symbol}USD",
                    exchange="Extended",
                    rate=fr,
                    price=price,
                    volume_24h=0,
                    interval_hours=1,
                    payments_per_day=24,
                    next_funding_ts=_calc_next_hourly_ts(),
                ))
            except Exception as e:
                log.debug(f"Extended {market} error: {e}")
                continue

        return rates

    # ── Helpers ───────────────────────────────────────────────────

    def fetch_funding_history(self, symbol: str, exchange: str,
                              limit: int = 15):
        """Stub: DeFi history not yet implemented. Return empty."""
        from core.models import FundingHistory
        return FundingHistory(symbol=symbol, exchange=exchange)

    def fetch_spot_availability(self, symbol: str, exchange: str) -> bool:
        """DeFi exchanges are perp-only, no spot."""
        return False
