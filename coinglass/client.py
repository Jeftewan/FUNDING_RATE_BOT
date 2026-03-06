"""Coinglass API integration for funding rate arbitrage data."""
import logging
import requests

log = logging.getLogger("bot")

BASE_URL = "https://open-api-v3.coinglass.com"


class CoinglassClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "CG-API-KEY": api_key,
        })

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make authenticated GET request to Coinglass API."""
        url = f"{BASE_URL}{endpoint}"
        try:
            r = self.session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == "0" or data.get("success"):
                return data.get("data", data)
            log.warning(f"Coinglass API error: {data.get('msg', 'unknown')}")
            return {}
        except requests.exceptions.Timeout:
            log.warning("Coinglass API timeout")
            return {}
        except requests.exceptions.RequestException as e:
            log.warning(f"Coinglass API error: {e}")
            return {}

    def fetch_arbitrage_opportunities(self) -> list:
        """Fetch funding rate arbitrage opportunities from Coinglass.

        Returns list of opportunities with pre-calculated metrics.
        """
        data = self._get("/api/futures/funding-rate/arbitrage")
        if not data:
            return []

        opportunities = []
        items = data if isinstance(data, list) else data.get("list", [])

        for item in items:
            try:
                opp = {
                    "source": "coinglass",
                    "symbol": item.get("symbol", ""),
                    "exchanges": item.get("exchangeList", []),
                    "current_rate": item.get("currentFundingRate", 0),
                    "accumulated_rate": item.get("accumulatedRate", 0),
                    "predicted_rate": item.get("predictedRate", 0),
                    "apr": item.get("apr", 0),
                    "market_cap": item.get("marketCap", 0),
                    "volume_24h": item.get("vol24h", 0),
                    "open_interest": item.get("openInterest", 0),
                }
                opportunities.append(opp)
            except Exception as e:
                log.warning(f"Coinglass parse error: {e}")
                continue

        return opportunities

    def fetch_funding_rates(self, symbol: str = None) -> list:
        """Fetch current funding rates across exchanges."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/futures/funding-rate/current", params=params)
        if not data:
            return []
        return data if isinstance(data, list) else data.get("list", [])

    def fetch_funding_history(self, symbol: str, exchange: str = None) -> list:
        """Fetch historical funding rates."""
        params = {"symbol": symbol}
        if exchange:
            params["exchange"] = exchange
        data = self._get("/api/futures/funding-rate/history", params=params)
        if not data:
            return []
        return data if isinstance(data, list) else data.get("list", [])

    def is_configured(self) -> bool:
        return bool(self.api_key)
