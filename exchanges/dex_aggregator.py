"""Agregadores DEX para la pierna spot on-chain (BSC).

Interfaz común (la usa onchain_bsc.BscSpotLeg):
  quote(src, dst, amount_wei, wallet)            → {"to_amount": int, "price_impact": float|None}
  swap_tx(src, dst, amount_wei, wallet, slip)    → {"tx": {...}, "min_out": int, "spender": str,
                                                    "price_impact": float|None}
  broadcast(raw_hex, wallet) (opcional)          → tx hash

Binance Wallet Web3 API (por defecto) — https://web3.binance.com/build
  Firma HMAC-SHA256 base64 de ts + METHOD + "/build" + path?query + body.
  ⚠ Bloquea por región del SERVIDOR (US, CA, NL, GB…; códigos 40301/40302/40304).
0x Swap API v2 (DEX_AGGREGATOR=0x, requiere ZEROX_API_KEY).
KyberSwap Aggregator (DEX_AGGREGATOR=kyber, sin API key). Además es el fallback
automático cuando el primario responde con bloqueo de región/compliance
(FallbackAggregator): el bot en Railway Amsterdam recibe 40304 de Binance Web3.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import requests

from exchanges.net import requests_proxies

log = logging.getLogger("bot.dex")

CHAIN_ID = "56"
# Binance Web3: 40301/40302 región bloqueada, 40304 "compliance restriction" (NL).
REGION_CODES = {40301, 40302, 40304}
REGION_TTL = 3600


class AggregatorError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class BinanceWeb3Aggregator:
    HOST = "https://web3.binance.com"
    PREFIX = "/build"

    def __init__(self, api_key: str, api_secret: str, mev_protection: bool = True):
        if not api_key or not api_secret:
            raise AggregatorError("Faltan BINANCE_WEB3_API_KEY / BINANCE_WEB3_API_SECRET")
        self.key = api_key
        self.secret = api_secret
        self.mev = mev_protection
        self._spenders = {}

    @staticmethod
    def _ts():
        n = datetime.now(timezone.utc)
        return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"

    def _call(self, method, path, params=None, body=None):
        qs = urlencode({k: v for k, v in (params or {}).items() if v is not None}, quote_via=quote)
        req_path = self.PREFIX + path + (f"?{qs}" if qs else "")
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        for attempt in range(3):
            ts = self._ts()
            sig = base64.b64encode(hmac.new(self.secret.encode(), (ts + method + req_path + body_str).encode(),
                                            hashlib.sha256).digest()).decode()
            headers = {"X-OC-APIKEY": self.key, "X-OC-TIMESTAMP": ts, "X-OC-SIGN": sig,
                       "X-OC-RECV-WINDOW": "10000", "Content-Type": "application/json"}
            # Firmar y enviar EXACTAMENTE los mismos bytes (no usar params=).
            r = requests.request(method, self.HOST + req_path, data=body_str or None,
                                 headers=headers, timeout=15, proxies=requests_proxies())
            if r.status_code == 429 and attempt < 2:
                time.sleep(float(r.headers.get("Retry-After", 1)))
                continue
            try:
                j = r.json()
            except ValueError:
                raise AggregatorError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400 or j.get("code") != 0:
                code = j.get("code")
                hint = " (región del servidor bloqueada por Binance Web3)" if code in REGION_CODES else ""
                raise AggregatorError(f"Web3 API {code}: {j.get('msg')}{hint}", code=code)
            return j.get("data")
        raise AggregatorError("Web3 API: rate limit")

    @staticmethod
    def _impact(route):
        v = route.get("priceImpactPercent")
        return abs(float(v)) / 100 if v not in (None, "") else None

    def _best_quote(self, src, dst, amount_wei):
        routes = self._call("GET", "/api/v1/dex/aggregator/quote", {
            "binanceChainId": CHAIN_ID, "amount": str(int(amount_wei)),
            "fromTokenAddress": src, "toTokenAddress": dst})
        routes = routes if isinstance(routes, list) else [routes]
        routes = [r for r in routes if r and r.get("executionMode", "SWAP") == "SWAP"]
        if not routes:
            raise AggregatorError("sin ruta de swap")
        for r in routes:
            for side in ("fromToken", "toToken"):
                if (r.get(side) or {}).get("isHoneyPot"):
                    raise AggregatorError("token marcado como honeypot")
        return next((r for r in routes if r.get("isBest")), routes[0])

    def quote(self, src, dst, amount_wei, wallet):
        r = self._best_quote(src, dst, amount_wei)
        return {"to_amount": int(r["toTokenAmount"]), "price_impact": self._impact(r)}

    def spender(self, token, amount_wei):
        if token.lower() in self._spenders:
            return self._spenders[token.lower()]
        rows = self._call("GET", "/api/v1/dex/aggregator/approve-transaction", {
            "binanceChainId": CHAIN_ID, "tokenContractAddress": token,
            "approveAmount": str(int(amount_wei))})
        rows = rows if isinstance(rows, list) else [rows]
        sp = rows[0]["dexContractAddress"]
        self._spenders[token.lower()] = sp
        return sp

    def swap_tx(self, src, dst, amount_wei, wallet, slippage, max_impact=0.01):
        r = self._best_quote(src, dst, amount_wei)
        d = self._call("GET", "/api/v1/dex/aggregator/swap", {
            "binanceChainId": CHAIN_ID, "amount": str(int(amount_wei)),
            "fromTokenAddress": src, "toTokenAddress": dst, "userWalletAddress": wallet,
            "quoteId": r["quoteId"], "slippagePercent": f"{slippage * 100:g}",
            "priceImpactProtectionPercent": f"{max_impact * 100:g}"})
        if d.get("executionMode", "SWAP") != "SWAP":
            raise AggregatorError("ruta RFQ no soportada")
        tx = d["tx"]
        if (tx.get("from") or wallet).lower() != wallet.lower():
            raise AggregatorError("tx.from no coincide con la wallet")
        return {"tx": {"to": tx["to"], "data": tx["data"], "value": int(tx.get("value") or 0),
                       "gas": int(tx["gas"]) if tx.get("gas") else None,
                       "gasPrice": int(tx["gasPrice"]) if tx.get("gasPrice") else None,
                       "maxPriorityFeePerGas": int(tx["maxPriorityFeePerGas"])
                       if tx.get("maxPriorityFeePerGas") else None},
                "min_out": int(tx.get("minReceiveAmount") or 0),
                "spender": self.spender(src, amount_wei),
                "price_impact": self._impact(d.get("routerResult") or r)}

    def broadcast(self, raw_hex, wallet):
        d = self._call("POST", "/api/v1/dex/pre-transaction/broadcast-transaction", body={
            "binanceChainId": CHAIN_ID, "signedTransaction": raw_hex, "address": wallet,
            "enableMevProtection": self.mev})
        return d["txHash"]


class ZeroExAggregator:
    """0x Swap API v2 (allowance-holder). Campos marcados sin verificar en el
    research: validar con un swap pequeño antes de usarlo en producción."""

    BASE = "https://api.0x.org/swap/allowance-holder"

    def __init__(self, api_key: str):
        if not api_key:
            raise AggregatorError("Falta ZEROX_API_KEY")
        self.h = {"0x-api-key": api_key, "0x-version": "v2"}

    def _get(self, path, params):
        r = requests.get(self.BASE + path, params={"chainId": CHAIN_ID, **params}, headers=self.h, timeout=15,
                         proxies=requests_proxies())
        j = r.json()
        if r.status_code >= 400:
            raise AggregatorError(f"0x {r.status_code}: {j}")
        return j

    def quote(self, src, dst, amount_wei, wallet):
        j = self._get("/price", {"sellToken": src, "buyToken": dst, "sellAmount": str(int(amount_wei)),
                                 "taker": wallet})
        return {"to_amount": int(j["buyAmount"]), "price_impact": None}

    def _quote_full(self, src, dst, amount_wei, wallet, slippage):
        return self._get("/quote", {"sellToken": src, "buyToken": dst, "sellAmount": str(int(amount_wei)),
                                    "taker": wallet, "slippageBps": int(slippage * 10000)})

    def swap_tx(self, src, dst, amount_wei, wallet, slippage, max_impact=0.01):
        j = self._quote_full(src, dst, amount_wei, wallet, slippage)
        allow = (j.get("issues") or {}).get("allowance") or {}
        t = j["transaction"]
        return {"tx": {"to": t["to"], "data": t["data"], "value": int(t.get("value") or 0),
                       "gas": int(t["gas"]) if t.get("gas") else None,
                       "gasPrice": int(t["gasPrice"]) if t.get("gasPrice") else None},
                "min_out": int(j.get("minBuyAmount") or 0), "price_impact": None,
                "spender": allow.get("spender") or t["to"]}


class KyberSwapAggregator:
    """KyberSwap Aggregator API v1 (BSC). Sin API key; `x-client-id` identifica
    al integrador. Sin broadcast propio → la tx se envía por el RPC BSC."""

    BASE = "https://aggregator-api.kyberswap.com/bsc/api/v1"
    CLIENT_ID = "basyo"

    def __init__(self):
        self.h = {"x-client-id": self.CLIENT_ID}

    def _req(self, method, path, params=None, body=None):
        r = requests.request(method, self.BASE + path, params=params, json=body, headers=self.h,
                             timeout=15)
        try:
            j = r.json()
        except ValueError:
            raise AggregatorError(f"Kyber HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400 or j.get("code") not in (0, None):
            raise AggregatorError(f"Kyber {j.get('code')}: {j.get('message')}", code=j.get("code"))
        return j.get("data") or {}

    @staticmethod
    def _impact(d):
        try:
            usd_in, usd_out = float(d.get("amountInUsd") or 0), float(d.get("amountOutUsd") or 0)
        except (TypeError, ValueError):
            return None
        return max(0.0, 1 - usd_out / usd_in) if usd_in > 0 and usd_out > 0 else None

    def _route(self, src, dst, amount_wei):
        d = self._req("GET", "/routes", {"tokenIn": src, "tokenOut": dst, "amountIn": str(int(amount_wei))})
        if not d.get("routeSummary"):
            raise AggregatorError("sin ruta de swap")
        return d

    def quote(self, src, dst, amount_wei, wallet):
        s = self._route(src, dst, amount_wei)["routeSummary"]
        return {"to_amount": int(s["amountOut"]), "price_impact": self._impact(s)}

    def swap_tx(self, src, dst, amount_wei, wallet, slippage, max_impact=0.01):
        route = self._route(src, dst, amount_wei)
        impact = self._impact(route["routeSummary"])
        if impact is not None and impact > max_impact:
            raise AggregatorError(f"impacto de precio {impact * 100:.2f}% > máx {max_impact * 100:.1f}%")
        d = self._req("POST", "/route/build", body={
            "routeSummary": route["routeSummary"], "sender": wallet, "recipient": wallet,
            "slippageTolerance": int(round(slippage * 10000)), "source": self.CLIENT_ID})
        router = d.get("routerAddress") or route.get("routerAddress")
        if not router or not d.get("data"):
            raise AggregatorError("Kyber route/build sin calldata")
        amount_out = int(d.get("amountOut") or route["routeSummary"]["amountOut"])
        return {"tx": {"to": router, "data": d["data"], "value": int(d.get("transactionValue") or 0),
                       "gas": int(d["gas"]) if d.get("gas") else None, "gasPrice": None},
                "min_out": int(amount_out * (1 - slippage)), "spender": router,
                "price_impact": self._impact(d) if d.get("amountInUsd") else impact}


class FallbackAggregator:
    """Primario + secundarios. Ante un bloqueo de región/compliance del primario
    (Binance Web3 40301/40302/40304) cambia al siguiente y recuerda el bloqueo
    REGION_TTL segundos para no pagar el round-trip fallido en cada swap."""

    def __init__(self, chain):
        self.chain = [a for a in chain if a is not None]
        self._blocked = {}   # idx → ts
        self._last = None    # agregador que armó la última tx (para broadcast)

    def _usable(self):
        now = time.time()
        items = [(i, a) for i, a in enumerate(self.chain) if now - self._blocked.get(i, 0) >= REGION_TTL]
        return items or list(enumerate(self.chain[-1:], start=len(self.chain) - 1))

    def _call(self, name, *args, **kw):
        err = None
        for i, agg in self._usable():
            try:
                out = getattr(agg, name)(*args, **kw)
                if name == "swap_tx":
                    self._last = agg
                return out
            except AggregatorError as e:
                if e.code not in REGION_CODES or i == len(self.chain) - 1:
                    raise
                self._blocked[i] = time.time()
                log.warning(f"{type(agg).__name__} bloqueado por región ({e}); fallback a siguiente agregador")
                err = e
        raise err or AggregatorError("sin agregador disponible")

    def quote(self, *a, **kw):
        return self._call("quote", *a, **kw)

    def swap_tx(self, *a, **kw):
        return self._call("swap_tx", *a, **kw)

    def broadcast(self, raw_hex, wallet):
        if self._last is None or not hasattr(self._last, "broadcast"):
            raise AggregatorError("agregador sin broadcast (se usa RPC)")
        return self._last.broadcast(raw_hex, wallet)


def build_aggregator():
    kind = os.environ.get("DEX_AGGREGATOR", "binance").lower()
    if kind == "kyber":
        return KyberSwapAggregator()
    try:
        if kind == "0x":
            primary = ZeroExAggregator(os.environ.get("ZEROX_API_KEY", ""))
        else:
            primary = BinanceWeb3Aggregator(os.environ.get("BINANCE_WEB3_API_KEY", ""),
                                            os.environ.get("BINANCE_WEB3_API_SECRET", ""),
                                            mev_protection=os.environ.get("BSC_MEV_PROTECTION", "1") != "0")
    except AggregatorError as e:
        log.warning(f"agregador '{kind}' no disponible ({e}); se usa KyberSwap")
        return KyberSwapAggregator()
    return FallbackAggregator([primary, KyberSwapAggregator()])
