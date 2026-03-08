"""Unified exchange access via CCXT for Binance, Bybit, OKX, Bitget."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.models import FundingRate, FundingHistory

log = logging.getLogger("bot")

# Exchange display names mapping
EXCHANGE_NAMES = {
    "binance": "Binance",
    "bybit": "Bybit",
    "okx": "OKX",
    "bitget": "Bitget",
}


class ExchangeManager:
    def __init__(self, config):
        self.config = config
        self._exchanges = {}
        self._spot_caches = {}  # {exchange: set of spot symbols}
        self._init_exchanges()

    def _init_exchanges(self):
        try:
            import ccxt
        except ImportError:
            log.error("ccxt not installed. Run: pip install ccxt")
            return

        exchange_configs = {
            "binance": {
                "class": ccxt.binance,
                "options": {"defaultType": "swap"},
                "api_key": self.config.BINANCE_API_KEY,
                "secret": self.config.BINANCE_API_SECRET,
            },
            "bybit": {
                "class": ccxt.bybit,
                "options": {"defaultType": "swap"},
                "api_key": self.config.BYBIT_API_KEY,
                "secret": self.config.BYBIT_API_SECRET,
            },
            "okx": {
                "class": ccxt.okx,
                "options": {"defaultType": "swap"},
                "api_key": self.config.OKX_API_KEY,
                "secret": self.config.OKX_API_SECRET,
                "password": self.config.OKX_PASSPHRASE,
            },
            "bitget": {
                "class": ccxt.bitget,
                "options": {"defaultType": "swap"},
                "api_key": self.config.BITGET_API_KEY,
                "secret": self.config.BITGET_API_SECRET,
                "password": self.config.BITGET_PASSPHRASE,
            },
        }

        for name in self.config.ENABLED_EXCHANGES:
            name = name.strip().lower()
            if name not in exchange_configs:
                log.warning(f"Unknown exchange: {name}")
                continue
            cfg = exchange_configs[name]
            try:
                params = {"enableRateLimit": True, "options": cfg["options"]}
                if cfg.get("api_key"):
                    params["apiKey"] = cfg["api_key"]
                    params["secret"] = cfg["secret"]
                if cfg.get("password"):
                    params["password"] = cfg["password"]
                self._exchanges[name] = cfg["class"](params)
                log.info(f"Exchange initialized: {EXCHANGE_NAMES.get(name, name)}")
            except Exception as e:
                log.error(f"Failed to init {name}: {e}")

    def get_exchange(self, name: str):
        return self._exchanges.get(name.lower())

    def fetch_all_funding_rates(self) -> dict:
        """Fetch funding rates from all exchanges in parallel.
        Returns {exchange_name: [FundingRate, ...]}"""
        results = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(self._fetch_exchange_rates, name): name
                for name in self._exchanges
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    rates = future.result(timeout=30)
                    results[name] = rates
                    log.info(f"{EXCHANGE_NAMES.get(name, name)}: {len(rates)} pairs")
                except Exception as e:
                    log.error(f"Fetch failed for {name}: {e}")
                    results[name] = []
        return results

    def _fetch_exchange_rates(self, exchange_name: str) -> list:
        """Fetch funding rates for a single exchange using CCXT."""
        ex = self._exchanges[exchange_name]
        display = EXCHANGE_NAMES.get(exchange_name, exchange_name)
        rates = []

        try:
            # Load markets if not loaded
            if not ex.markets:
                ex.load_markets()

            # Fetch funding rates
            funding_rates = ex.fetch_funding_rates()

            for symbol, data in funding_rates.items():
                # Only USDT pairs
                info = ex.market(symbol) if symbol in ex.markets else None
                if not info:
                    continue
                base = info.get("base", "")
                quote = info.get("quote", "")
                if quote != "USDT":
                    continue

                rate = data.get("fundingRate")
                if rate is None:
                    continue

                mark_price = data.get("markPrice", 0) or 0
                next_ts = data.get("fundingTimestamp", 0) or 0

                # Determine funding interval from CCXT unified response
                ih = self._get_funding_interval(exchange_name, info, data)
                ipd = 24 / ih if ih > 0 else 3

                # Minutes to next funding
                mins_next = -1
                if next_ts and next_ts > 0:
                    mins_next = max(0, (next_ts / 1000 - time.time()) / 60)

                # Get 24h volume — try from ticker if not in funding data
                volume = 0
                try:
                    if hasattr(data, "get"):
                        volume = data.get("quoteVolume", 0) or 0
                except (KeyError, TypeError):
                    pass

                pair = f"{base}USDT"
                rates.append(FundingRate(
                    symbol=base, pair=pair, exchange=display,
                    rate=float(rate), price=float(mark_price),
                    volume_24h=float(volume),
                    interval_hours=ih, payments_per_day=ipd,
                    next_funding_ts=int(next_ts) if next_ts else 0,
                    mins_to_next=mins_next,
                ))

        except Exception as e:
            log.error(f"Error fetching {display} rates: {e}")

        # If volume data is missing, try fetching tickers
        if rates and all(r.volume_24h == 0 for r in rates):
            self._enrich_volumes(exchange_name, rates)

        return rates

    def _get_funding_interval(self, exchange_name: str, market_info: dict,
                              funding_data: dict) -> int:
        """Determine funding interval in hours.

        Uses the unified CCXT 'interval' field first (e.g. '8h', '4h', '1h'),
        then falls back to exchange-specific raw fields in market info.
        """
        # 1. Check unified CCXT 'interval' field from fetch_funding_rates()
        #    Works for Bybit, OKX, Bitget; None for Binance
        if funding_data:
            interval_str = funding_data.get("interval")
            if interval_str and isinstance(interval_str, str):
                hours = interval_str.replace("h", "")
                try:
                    return int(hours)
                except (ValueError, TypeError):
                    pass

        # 2. Fall back to exchange-specific raw fields in market info
        if market_info:
            info = market_info.get("info", {})
            if isinstance(info, dict):
                # Bybit: fundingInterval in minutes (60, 240, 480)
                fund_interval = info.get("fundingInterval")
                if fund_interval:
                    try:
                        return int(fund_interval) // 60
                    except (ValueError, TypeError):
                        pass
                # Bitget: fundInterval in hours as string ("1", "4", "8")
                fund_iv = info.get("fundInterval")
                if fund_iv:
                    try:
                        return int(fund_iv)
                    except (ValueError, TypeError):
                        pass

        return 8

    def _enrich_volumes(self, exchange_name: str, rates: list):
        """Fetch 24h volumes via tickers if not available from funding data."""
        ex = self._exchanges[exchange_name]
        try:
            tickers = ex.fetch_tickers()
            vol_map = {}
            for sym, ticker in tickers.items():
                info = ex.market(sym) if sym in ex.markets else None
                if info and info.get("quote") == "USDT":
                    base = info.get("base", "")
                    vol_map[base] = float(ticker.get("quoteVolume", 0) or 0)

            for rate in rates:
                if rate.symbol in vol_map:
                    rate.volume_24h = vol_map[rate.symbol]
        except Exception as e:
            log.warning(f"Volume enrichment failed for {exchange_name}: {e}")

    def fetch_funding_history(self, symbol: str, exchange_name: str,
                              limit: int = 30) -> FundingHistory:
        """Fetch historical funding rates for a symbol."""
        exchange_name = exchange_name.lower()
        # Map display names back
        name_map = {v.lower(): k for k, v in EXCHANGE_NAMES.items()}
        exchange_name = name_map.get(exchange_name, exchange_name)

        ex = self._exchanges.get(exchange_name)
        if not ex:
            return FundingHistory(symbol=symbol, exchange=exchange_name)

        display = EXCHANGE_NAMES.get(exchange_name, exchange_name)
        pair = f"{symbol}/USDT:USDT"

        try:
            if not ex.markets:
                ex.load_markets()

            history = ex.fetch_funding_rate_history(pair, limit=limit)
            if not history:
                return FundingHistory(symbol=symbol, exchange=display)

            rates = [float(h.get("fundingRate", 0)) for h in history]
            timestamps = [int(h.get("timestamp", 0)) for h in history]

            # Reverse if newest first (some exchanges)
            if len(timestamps) >= 2 and timestamps[0] > timestamps[-1]:
                rates.reverse()
                timestamps.reverse()

            return self._build_history(symbol, display, rates, timestamps)

        except Exception as e:
            log.warning(f"History fetch failed {symbol}@{display}: {e}")
            return FundingHistory(symbol=symbol, exchange=display)

    def _build_history(self, symbol: str, exchange: str,
                       rates: list, timestamps: list) -> FundingHistory:
        """Build FundingHistory with stats from raw rates."""
        import math
        if not rates:
            return FundingHistory(symbol=symbol, exchange=exchange)

        avg = sum(rates) / len(rates)
        variance = sum((r - avg) ** 2 for r in rates) / len(rates)
        stddev = math.sqrt(variance)

        # Determine direction from most recent rate
        fr_sign = 1 if rates[-1] >= 0 else -1
        fav = sum(1 for r in rates
                  if (fr_sign > 0 and r > 0) or (fr_sign < 0 and r < 0))
        fav_pct = fav / len(rates) * 100

        streak = 0
        for r in reversed(rates):
            if (fr_sign > 0 and r > 0) or (fr_sign < 0 and r < 0):
                streak += 1
            else:
                break

        return FundingHistory(
            symbol=symbol, exchange=exchange,
            rates=rates, timestamps=timestamps,
            avg=avg, stddev=stddev,
            consistency_pct=fav_pct,
            streak=streak, favorable_pct=fav_pct,
        )

    def fetch_spot_availability(self, symbol: str,
                                exchange_name: str) -> bool:
        """Check if a spot pair exists on the given exchange."""
        exchange_name_lower = exchange_name.lower()
        name_map = {v.lower(): k for k, v in EXCHANGE_NAMES.items()}
        exchange_name_lower = name_map.get(exchange_name_lower, exchange_name_lower)

        # Use cache
        if exchange_name_lower in self._spot_caches:
            return symbol in self._spot_caches[exchange_name_lower]

        ex = self._exchanges.get(exchange_name_lower)
        if not ex:
            return False

        try:
            # Create a spot exchange instance
            import ccxt
            spot_configs = {
                "binance": ccxt.binance,
                "bybit": ccxt.bybit,
                "okx": ccxt.okx,
                "bitget": ccxt.bitget,
            }
            cls = spot_configs.get(exchange_name_lower)
            if not cls:
                return True  # Assume available if unknown

            spot_ex = cls({"enableRateLimit": True, "options": {"defaultType": "spot"}})
            spot_ex.load_markets()

            spot_symbols = set()
            for sym, info in spot_ex.markets.items():
                if info.get("quote") == "USDT" and info.get("active", True):
                    spot_symbols.add(info.get("base", ""))

            self._spot_caches[exchange_name_lower] = spot_symbols
            log.info(f"{exchange_name} spot pairs cached: {len(spot_symbols)}")
            return symbol in spot_symbols

        except Exception as e:
            log.warning(f"Spot check failed for {exchange_name}: {e}")
            return True  # Assume available on error

    def fetch_klines(self, symbol: str, exchange_name: str,
                     interval: str = "1d", limit: int = 16) -> list:
        """Fetch OHLCV klines for RSI calculation."""
        exchange_name_lower = exchange_name.lower()
        name_map = {v.lower(): k for k, v in EXCHANGE_NAMES.items()}
        exchange_name_lower = name_map.get(exchange_name_lower, exchange_name_lower)

        ex = self._exchanges.get(exchange_name_lower)
        if not ex:
            return []

        pair = f"{symbol}/USDT:USDT"
        try:
            if not ex.markets:
                ex.load_markets()
            ohlcv = ex.fetch_ohlcv(pair, timeframe=interval, limit=limit)
            return ohlcv if ohlcv else []
        except Exception as e:
            log.warning(f"Klines failed {symbol}@{exchange_name}: {e}")
            return []

    def get_exchange_status(self) -> dict:
        """Check connectivity for all configured exchanges."""
        status = {}
        for name, ex in self._exchanges.items():
            display = EXCHANGE_NAMES.get(name, name)
            try:
                ex.fetch_time()
                status[display] = {"ok": True, "error": ""}
            except Exception as e:
                status[display] = {"ok": False, "error": str(e)[:100]}
        return status

    def detect_funding_interval_from_history(self, timestamps: list) -> int:
        """Detect actual funding interval from timestamp differences."""
        if len(timestamps) < 2:
            return 8
        diffs = []
        for i in range(min(3, len(timestamps) - 1)):
            diff_h = abs(timestamps[i] - timestamps[i + 1]) / (1000 * 3600)
            diffs.append(diff_h)
        avg = sum(diffs) / len(diffs) if diffs else 8
        for iv in [1, 2, 4, 8]:
            if abs(avg - iv) < 1:
                return iv
        return 8
