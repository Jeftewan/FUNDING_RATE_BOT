"""Unified exchange access via CCXT for Binance, Bybit, OKX, Bitget."""
import logging
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.models import FundingRate, FundingHistory
from exchanges.net import ccxt_proxy_params, is_geoblock_error, mark_geoblocked

log = logging.getLogger("bot")

# Exchange display names mapping
EXCHANGE_NAMES = {
    "binance": "Binance",
    "bybit": "Bybit",
    "okx": "OKX",
    "bitget": "Bitget",
}

# Minimum seconds between API calls per exchange
MIN_FETCH_INTERVAL = 120
# Binance cambia intervalos de funding (8h→4h→1h) sin aviso: /fapi/v1/fundingInfo
# se re-lee cada FUNDING_INTERVALS_TTL (antes se cacheaba hasta reiniciar).
FUNDING_INTERVALS_TTL = 900
# Historial de funding cacheado por símbolo: válido hasta el próximo settlement
# (+ margen para que el exchange publique la tasa liquidada).
HISTORY_SETTLE_GRACE_SECS = 180


class ExchangeManager:
    def __init__(self, config):
        self.config = config
        self._exchanges = {}
        self._spot_caches = {}  # {exchange: set of spot symbols}
        self._funding_intervals = {}  # {exchange: {symbol: hours}}
        self._funding_intervals_ts = {}  # {exchange: loaded_ts}
        self._history_cache = {}  # {(exchange, symbol, limit): (FundingHistory, valid_until)}
        self._history_lock = __import__("threading").Lock()
        # Rate limit / ban tracking
        self._last_fetch_ts = {}    # {exchange: timestamp}
        self._rate_cache = {}       # {exchange: [FundingRate, ...]}
        self._ban_until = {}        # {exchange: timestamp} — banned until
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
                "options": {"defaultType": "swap",
                            "fetchMarkets": {"types": ["spot", "linear"]}},
                "api_key": self.config.BINANCE_API_KEY,
                "secret": self.config.BINANCE_API_SECRET,
            },
            "bybit": {
                "class": ccxt.bybit,
                "options": {"defaultType": "swap",
                            "fetchMarkets": {"types": ["spot", "linear"]}},
                "api_key": self.config.BYBIT_API_KEY,
                "secret": self.config.BYBIT_API_SECRET,
            },
            "okx": {
                "class": ccxt.okx,
                "options": {"defaultType": "swap",
                            "fetchMarkets": {"types": ["spot", "swap"]}},
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
                params = {"enableRateLimit": True, "options": cfg["options"], **ccxt_proxy_params()}
                if cfg.get("api_key"):
                    params["apiKey"] = cfg["api_key"]
                    params["secret"] = cfg["secret"]
                if cfg.get("password"):
                    params["password"] = cfg["password"]
                self._exchanges[name] = cfg["class"](params)
                log.info(f"Exchange initialized: {EXCHANGE_NAMES.get(name, name)}")
            except Exception as e:
                log.error(f"Failed to init {name}: {e}")

        # Wire slippage + fee loader so analysis/fees.py can hit real data
        try:
            from analysis.slippage import bind_exchange_manager
            bind_exchange_manager(self)
        except Exception as e:
            log.debug(f"slippage bind skipped: {e}")

        try:
            from analysis.fee_loader import load_fees_async
            load_fees_async(self._exchanges, EXCHANGE_NAMES)
        except Exception as e:
            log.debug(f"fee_loader skipped: {e}")

    def get_exchange(self, name: str):
        return self._exchanges.get(name.lower())

    @staticmethod
    def _ensure_markets(ex):
        """load_markets una vez y descarta el JSON crudo de cada mercado.

        `market['info']` (respuesta cruda del exchange) es ~70% de la RAM de
        los mercados (Binance: ~105 MB → ~30 MB). Estas instancias son de solo
        lectura; ccxt solo lo usa al crear órdenes, que van por
        trade_executor con clientes propios. Se deja {} para que los
        safe_* de ccxt sigan funcionando.
        """
        if ex.markets:
            return
        ex.load_markets()
        for m in ex.markets.values():
            m["info"] = {}

    def fetch_all_funding_rates(self, force: bool = False) -> dict:
        """Fetch funding rates from all exchanges in parallel.
        Uses cache if data is fresh enough. Skips banned exchanges.
        Returns {exchange_name: [FundingRate, ...]}"""
        results = {}
        now = time.time()

        # Determine which exchanges need fresh fetch vs cache
        to_fetch = []
        for name in self._exchanges:
            # Check if banned
            ban_ts = self._ban_until.get(name, 0)
            if ban_ts > now:
                mins_left = (ban_ts - now) / 60
                if name not in self._rate_cache:
                    log.warning(f"{EXCHANGE_NAMES.get(name, name)}: banned for {mins_left:.0f}min, no cache")
                    results[name] = []
                else:
                    log.info(f"{EXCHANGE_NAMES.get(name, name)}: banned, using cache ({len(self._rate_cache[name])} pairs)")
                    results[name] = self._rate_cache[name]
                continue

            # Check cache freshness
            last_ts = self._last_fetch_ts.get(name, 0)
            if not force and (now - last_ts) < MIN_FETCH_INTERVAL and name in self._rate_cache:
                log.debug(f"{EXCHANGE_NAMES.get(name, name)}: using cache ({now - last_ts:.0f}s old)")
                results[name] = self._rate_cache[name]
                continue

            to_fetch.append(name)

        if to_fetch:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(self._fetch_exchange_rates, name): name
                    for name in to_fetch
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        rates = future.result(timeout=30)
                        results[name] = rates
                        self._rate_cache[name] = rates
                        self._last_fetch_ts[name] = time.time()
                        log.info(f"{EXCHANGE_NAMES.get(name, name)}: {len(rates)} pairs (fresh)")
                    except Exception as e:
                        err_str = str(e)
                        # Detect IP ban from error message
                        ban_ts = self._parse_ban(err_str)
                        if ban_ts:
                            self._ban_until[name] = ban_ts
                            mins = (ban_ts - time.time()) / 60
                            log.error(f"{EXCHANGE_NAMES.get(name, name)}: IP BANNED for {mins:.0f}min")
                        elif is_geoblock_error(err_str):
                            mark_geoblocked(name)
                            log.error(f"{EXCHANGE_NAMES.get(name, name)}: GEOBLOQUEADO desde la IP "
                                      "de salida (ver EXCHANGE_PROXY_URL)")
                        else:
                            log.error(f"Fetch failed for {name}: {e}")

                        # Use cache if available
                        if name in self._rate_cache:
                            results[name] = self._rate_cache[name]
                            log.info(f"{EXCHANGE_NAMES.get(name, name)}: using stale cache")
                        else:
                            results[name] = []

        return results

    def _parse_ban(self, error_msg: str) -> float:
        """Parse ban-until timestamp from exchange error. Returns unix ts or 0."""
        # Binance: "banned until 1773147057263"
        m = re.search(r'banned until (\d{13})', error_msg)
        if m:
            return int(m.group(1)) / 1000
        # Generic rate limit — back off 10 minutes
        if "too many" in error_msg.lower() or "rate limit" in error_msg.lower():
            return time.time() + 600
        if "418" in error_msg or "429" in error_msg:
            return time.time() + 600
        return 0

    def _fetch_exchange_rates(self, exchange_name: str) -> list:
        """Fetch funding rates for a single exchange using CCXT."""
        ex = self._exchanges[exchange_name]
        display = EXCHANGE_NAMES.get(exchange_name, exchange_name)
        rates = []

        try:
            self._ensure_markets(ex)

            # For Binance: fetch funding intervals (separate API call)
            # since fetch_funding_rates() doesn't include interval info
            if exchange_name == "binance" and (
                    time.time() - self._funding_intervals_ts.get(exchange_name, 0) > FUNDING_INTERVALS_TTL):
                try:
                    fi_data = ex.fetch_funding_intervals()
                    iv_map = {}
                    for sym, fi in fi_data.items():
                        interval_str = fi.get("interval")
                        if interval_str and isinstance(interval_str, str):
                            try:
                                iv_map[sym] = int(interval_str.replace("h", ""))
                            except (ValueError, TypeError):
                                pass
                    prev = self._funding_intervals.get("binance") or {}
                    changed = [s for s, h in iv_map.items() if prev and prev.get(s, 8) != h]
                    self._funding_intervals["binance"] = iv_map
                    self._funding_intervals_ts["binance"] = time.time()
                    log.info(f"Binance: cached {len(iv_map)} funding intervals"
                             + (f" ({len(changed)} cambiaron: {changed[:5]})" if changed else ""))
                except Exception as e:
                    # Se conserva el mapa anterior (mejor que volver a 8h para todos).
                    log.warning(f"Binance funding intervals fetch failed: {e}")
                    self._funding_intervals.setdefault("binance", {})

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
                ih = self._get_funding_interval(exchange_name, info, data, symbol)
                ipd = 24 / ih if ih > 0 else 3

                # If next funding timestamp is missing (e.g. Bitget bulk endpoint)
                # OR is in the past (some exchanges briefly return the LAST
                # payment time around rollover), calculate from the fixed UTC
                # schedule.  A past timestamp would otherwise freeze the
                # snapshot unique constraint (symbol, exchange, funding_ts).
                now_ms = int(time.time() * 1000)
                if not next_ts or next_ts <= 0 or next_ts <= now_ms:
                    next_ts = self._calc_next_funding_ts(ih)

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
            raise  # Re-raise so fetch_all can detect bans

        # If volume data is missing, try fetching tickers
        if rates and all(r.volume_24h == 0 for r in rates):
            self._enrich_volumes(exchange_name, rates)

        return rates

    def _get_funding_interval(self, exchange_name: str, market_info: dict,
                              funding_data: dict, symbol: str = None) -> int:
        """Determine funding interval in hours.

        Priority:
        1. Unified CCXT 'interval' from fetch_funding_rates() (Bybit, OKX, Bitget)
        2. Cached fetch_funding_intervals() data (Binance)
        3. Exchange-specific raw fields in market info (fallback)
        4. Default 8h
        """
        # 1. Check unified CCXT 'interval' field from fetch_funding_rates()
        if funding_data:
            interval_str = funding_data.get("interval")
            if interval_str and isinstance(interval_str, str):
                hours = interval_str.replace("h", "")
                try:
                    return int(hours)
                except (ValueError, TypeError):
                    pass

        # 2. Check cached funding intervals (from fetch_funding_intervals())
        if symbol and exchange_name in self._funding_intervals:
            cached = self._funding_intervals[exchange_name].get(symbol)
            if cached:
                return cached

        # 3. Fall back to exchange-specific raw fields in market info
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

    @staticmethod
    def _calc_next_funding_ts(interval_hours: int = 8) -> int:
        """Calculate next funding timestamp (ms) from fixed UTC schedule.

        Most exchanges use fixed intervals aligned to 00:00 UTC.
        E.g. 8h → 00:00, 08:00, 16:00 UTC; 4h → 00:00, 04:00, ...
        """
        import math
        now = time.time()
        interval_sec = interval_hours * 3600
        # Seconds since midnight UTC
        day_start = (now // 86400) * 86400
        elapsed = now - day_start
        # Next slot within the day
        next_slot = math.ceil(elapsed / interval_sec) * interval_sec
        next_time = day_start + next_slot
        # If rounding landed exactly on now, advance one interval
        if next_time <= now:
            next_time += interval_sec
        return int(next_time * 1000)

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
                              limit: int = 30, use_cache: bool = True) -> FundingHistory:
        """Fetch historical funding rates for a symbol.

        Cacheado por (exchange, símbolo, limit) hasta el próximo settlement del
        par (+HISTORY_SETTLE_GRACE_SECS): entre pagos el historial no cambia, así
        que un scan horario sobre ~150 candidatos casi no gasta llamadas.
        """
        exchange_name = exchange_name.lower()
        # Map display names back
        name_map = {v.lower(): k for k, v in EXCHANGE_NAMES.items()}
        exchange_name = name_map.get(exchange_name, exchange_name)

        ex = self._exchanges.get(exchange_name)
        if not ex:
            return FundingHistory(symbol=symbol, exchange=exchange_name)

        display = EXCHANGE_NAMES.get(exchange_name, exchange_name)
        pair = f"{symbol}/USDT:USDT"
        key = (exchange_name, symbol, int(limit))
        if use_cache:
            with self._history_lock:
                hit = self._history_cache.get(key)
            if hit and time.time() < hit[1]:
                return hit[0]

        try:
            self._ensure_markets(ex)

            history = ex.fetch_funding_rate_history(pair, limit=limit)
            if not history:
                return FundingHistory(symbol=symbol, exchange=display)

            rates = [float(h.get("fundingRate", 0)) for h in history]
            timestamps = [int(h.get("timestamp", 0)) for h in history]

            # Reverse if newest first (some exchanges)
            if len(timestamps) >= 2 and timestamps[0] > timestamps[-1]:
                rates.reverse()
                timestamps.reverse()

            hist = self._build_history(symbol, display, rates, timestamps)
            if len(timestamps) >= 2:
                ih_ms = max(3600_000, timestamps[-1] - timestamps[-2])
                # Tope 1 h: si el intervalo se acortó (8h→1h) el próximo pago llega antes.
                valid_until = min((timestamps[-1] + ih_ms) / 1000 + HISTORY_SETTLE_GRACE_SECS,
                                  time.time() + 3600)
                with self._history_lock:
                    if len(self._history_cache) > 2000:
                        self._history_cache.clear()
                    self._history_cache[key] = (hist, valid_until)
            return hist

        except Exception as e:
            log.warning(f"History fetch failed {symbol}@{display}: {e}")
            return FundingHistory(symbol=symbol, exchange=display)

    def fetch_settlement_rates(self, symbol: str, exchange_name: str, target_ts_list: list,
                               tolerance_secs: int = 120) -> dict:
        """{target_ts: tasa liquidada | None} con UNA llamada sin caché (justo
        después de un settlement el historial cacheado aún no lo tiene)."""
        n = max(10, len(target_ts_list) + 3)
        history = self.fetch_funding_history(symbol, exchange_name, limit=n, use_cache=False)
        out = {}
        for target_ts in target_ts_list:
            out[target_ts] = None
            if not history or not history.timestamps:
                continue
            target_ms = int(target_ts * 1000)
            best = min(zip(history.rates, history.timestamps),
                       key=lambda rt: abs(int(rt[1]) - target_ms))
            if abs(int(best[1]) - target_ms) <= tolerance_secs * 1000:
                out[target_ts] = float(best[0])
        return out

    def fetch_settlement_rate(self, symbol: str, exchange_name: str,
                              target_ts: float,
                              tolerance_secs: int = 120) -> float | None:
        """Return the historic funding rate closest to target_ts (seconds, UTC).

        Uses CCXT fetch_funding_rate_history (sin caché).
        Returns None if no entry within tolerance.
        """
        return self.fetch_settlement_rates(symbol, exchange_name, [target_ts],
                                           tolerance_secs).get(target_ts)

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
            # Reusa los mercados de la instancia principal (ya incluyen spot)
            # en vez de crear y cargar una segunda instancia completa.
            self._ensure_markets(ex)

            # Solo mercados SPOT: los linear swaps también tienen quote USDT y
            # antes marcaban has_spot=True para tokens sin spot (solo Alpha/Web3).
            spot_symbols = set()
            for sym, info in ex.markets.items():
                if (info.get("spot") and info.get("quote") == "USDT"
                        and info.get("active", True)):
                    spot_symbols.add(info.get("base", ""))

            self._spot_caches[exchange_name_lower] = spot_symbols
            log.info(f"{exchange_name} spot pairs cached: {len(spot_symbols)}")
            return symbol in spot_symbols

        except Exception as e:
            # Sin cache: el próximo scan reintenta. Ante la duda NO se muestra el
            # par como cubrible (el scan filtra por spot operable).
            log.warning(f"Spot check failed for {exchange_name}: {e}")
            return False

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
            self._ensure_markets(ex)
            ohlcv = ex.fetch_ohlcv(pair, timeframe=interval, limit=limit)
            return ohlcv if ohlcv else []
        except Exception as e:
            log.warning(f"Klines failed {symbol}@{exchange_name}: {e}")
            return []

    def get_exchange_status(self) -> dict:
        """Check connectivity for all configured exchanges."""
        status = {}
        now = time.time()
        for name, ex in self._exchanges.items():
            display = EXCHANGE_NAMES.get(name, name)
            ban_ts = self._ban_until.get(name, 0)
            if ban_ts > now:
                mins = (ban_ts - now) / 60
                status[display] = {"ok": False, "error": f"IP banned — {mins:.0f}min restantes"}
                continue
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
