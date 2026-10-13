"""Agregadores DEX para la pierna spot on-chain (BSC).

Interfaz común (la usa onchain_bsc.BscSpotLeg):
  quote(src, dst, amount_wei, wallet)            → {"to_amount": int, "price_impact": float|None}
  swap_tx(src, dst, amount_wei, wallet, slip)    → {"tx": {...}, "min_out": int, "spender": str,
                                                    "price_impact": float|None}
  broadcast(raw_hex, wallet) (opcional)          → tx hash

Binance Wallet Web3 API (por defecto) — https://web3.binance.com/build
  Firma HMAC-SHA256 base64 de ts + METHOD + "/build" + path?query + body.
  ⚠ Bloquea por región del SERVIDOR (US, CA, NL, GB…): desplegar el bot en una
  región permitida (p.ej. Railway asia-southeast/Singapur) o usar 0x.
0x Swap API v2 (fallback, DEX_AGGREGATOR=0x).
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

log = logging.getLogger("bot.dex")

CHAIN_ID = "56"


class AggregatorError(RuntimeError):
    pass


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
                                 headers=headers, timeout=15)
            if r.status_code == 429 and attempt < 2:
                time.sleep(float(r.headers.get("Retry-After", 1)))
                continue
            try:
                j = r.json()
            except ValueError:
                raise AggregatorError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code >= 400 or j.get("code") != 0:
                code = j.get("code")
                hint = " (región del servidor bloqueada por Binance Web3)" if code in (40301, 40302) else ""
                raise AggregatorError(f"Web3 API {code}: {j.get('msg')}{hint}")
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
        r = requests.get(self.BASE + path, params={"chainId": CHAIN_ID, **params}, headers=self.h, timeout=15)
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


def build_aggregator():
    kind = os.environ.get("DEX_AGGREGATOR", "binance").lower()
    if kind == "0x":
        return ZeroExAggregator(os.environ.get("ZEROX_API_KEY", ""))
    return BinanceWeb3Aggregator(os.environ.get("BINANCE_WEB3_API_KEY", ""),
                                 os.environ.get("BINANCE_WEB3_API_SECRET", ""),
                                 mev_protection=os.environ.get("BSC_MEV_PROTECTION", "1") != "0")
