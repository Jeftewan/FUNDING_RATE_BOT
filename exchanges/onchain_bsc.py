"""Pierna spot ON-CHAIN en BSC para tokens que en Binance solo existen en Alpha.

Binance no expone API para operar Binance Alpha ni la Web3 Wallet (MPC) del
usuario. Alternativa adoptada: el usuario registra una wallet BSC DEDICADA
(private key cifrada con Fernet, fail-closed) y basyo compra/vende el spot vía
un agregador DEX, mientras el short va al perp de Binance (trade_executor).

Flujo por tramo (BscSpotLeg.trade):
  quote → guardas (impacto de precio, basis vs perp) → approve acotado al monto
  (si hace falta) → swap firmado localmente → broadcast → receipt → cantidad
  REAL = delta de balanceOf → on_fill() → el caller cubre el perp por esa cantidad.

Sin web3.py (RAM): firma con eth-account y JSON-RPC vía requests.
Habilitado con ONCHAIN_ENABLED=1 (+ credenciales del agregador).
"""
import logging
import os
import time

import requests

from exchanges.net import requests_proxies

log = logging.getLogger("bot.bsc")

CHAIN_ID = 56
USDT = "0x55d398326f99059fF775485246999027B3197955"  # BSC-USD (Binance-Peg USDT), 18 dec
USDT_DECIMALS = 18
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
DEFAULT_SLIPPAGE = 0.005          # 0.5%
MAX_PRICE_IMPACT = 0.01           # 1%
MAX_PRICE_IMPACT_EXIT = 0.05      # vender spot desnudo (short ya cerrado): peor es quedarse sin cobertura
ONCHAIN_MAX_BASIS = 0.005         # compra on-chain hasta 0.5% sobre el perp bid (incluye fee del DEX)
MIN_LIQUIDITY_USD = 200_000       # liquidez mínima del token (lista Alpha)
RECEIPT_TIMEOUT = 90
GAS_BUFFER = 1.3                  # margen sobre eth_estimateGas (el estado cambia entre estimar y minar)
ALPHA_LIST_URL = ("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/"
                  "wallet/cex/alpha/all/token/list")
_ALPHA_TTL = 3600
_alpha_cache = {"ts": 0.0, "by_symbol": {}}

_SEL_BALANCE_OF = "0x70a08231"
_SEL_ALLOWANCE = "0xdd62ed3e"
_SEL_APPROVE = "0x095ea7b3"


def enabled() -> bool:
    return os.environ.get("ONCHAIN_ENABLED", "").lower() in ("1", "true", "yes")


def rpc_url() -> str:
    return os.environ.get("BSC_RPC_URL", "https://bsc-dataseed.bnbchain.org")


# ── Llaves ──────────────────────────────────────────────────────────────────
def normalize_key(pk: str) -> str:
    pk = (pk or "").strip()
    if pk.startswith("0x") or pk.startswith("0X"):
        pk = pk[2:]
    if len(pk) != 64 or any(c not in "0123456789abcdefABCDEF" for c in pk):
        raise ValueError("private key inválida")
    return "0x" + pk.lower()


def address_from_key(pk: str) -> str:
    from eth_account import Account
    return Account.from_key(normalize_key(pk)).address


# ── JSON-RPC ────────────────────────────────────────────────────────────────
def _rpc(method: str, params: list):
    r = requests.post(rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method,
                                       "params": params}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("error"):
        raise RuntimeError(f"RPC {method}: {j['error']}")
    return j.get("result")


def _addr_arg(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def erc20_balance(token: str, owner: str) -> int:
    res = _rpc("eth_call", [{"to": token, "data": _SEL_BALANCE_OF + _addr_arg(owner)}, "latest"])
    return int(res or "0x0", 16)


def erc20_allowance(token: str, owner: str, spender: str) -> int:
    data = _SEL_ALLOWANCE + _addr_arg(owner) + _addr_arg(spender)
    return int(_rpc("eth_call", [{"to": token, "data": data}, "latest"]) or "0x0", 16)


def bnb_balance(owner: str) -> int:
    return int(_rpc("eth_getBalance", [owner, "latest"]) or "0x0", 16)


def _nonce(owner: str) -> int:
    return int(_rpc("eth_getTransactionCount", [owner, "pending"]), 16)


def _gas_price() -> int:
    return int(_rpc("eth_gasPrice", []), 16)


def _estimate_gas(tx: dict) -> int:
    return int(_rpc("eth_estimateGas", [{k: tx[k] for k in ("from", "to", "data", "value") if k in tx}]), 16)


def _gas_limit(sender: str, tx: dict, hint=None) -> int:
    """máx(eth_estimateGas × GAS_BUFFER, gas sugerido por el agregador).

    El `gas` del agregador se queda corto en rutas multi-hop (caso real: límite
    250k, el swap necesitaba ~364k → revert por out-of-gas). La estimación además
    simula la tx: si revertiría, lanza y no se transmite (no se quema gas).
    """
    est = _estimate_gas({"from": sender, "to": tx["to"], "data": tx["data"], "value": hex(tx["value"])})
    return max(int(est * GAS_BUFFER), int(hint or 0))


def wait_receipt(tx_hash: str, timeout: float = RECEIPT_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = _rpc("eth_getTransactionReceipt", [tx_hash])
        if rc:
            return rc
        time.sleep(1)
    raise TimeoutError(f"tx {tx_hash} sin receipt en {timeout}s")


def to_wei(amount: float, decimals: int) -> int:
    """Decimal desde el repr (sin ruido binario de `amount * 10**18`), truncado.
    Un float que viene de from_wei(saldo) aún puede quedar unos wei por encima del
    saldo (caso real: +25 614 wei → TRANSFER_FROM_FAILED): quien vende "todo" debe
    acotar contra erc20_balance (ver BscSpotLeg._swap)."""
    from decimal import ROUND_DOWN, Decimal
    return int((Decimal(repr(float(amount))) * (10 ** decimals)).to_integral_value(rounding=ROUND_DOWN))


def from_wei(amount: int, decimals: int) -> float:
    return amount / (10 ** decimals)


# ── Tokens Alpha (BSC) ──────────────────────────────────────────────────────
def _load_alpha():
    if time.time() - _alpha_cache["ts"] < _ALPHA_TTL and _alpha_cache["by_symbol"]:
        return _alpha_cache["by_symbol"]
    try:
        r = requests.get(ALPHA_LIST_URL, timeout=20, proxies=requests_proxies())
        r.raise_for_status()
        rows = r.json().get("data") or []
    except Exception as e:
        log.warning(f"alpha token list failed: {e}")
        return _alpha_cache["by_symbol"]
    by = {}
    for t in rows:
        if str(t.get("chainId")) != str(CHAIN_ID) or t.get("offline") or t.get("fullyDelisted"):
            continue
        sym = (t.get("symbol") or "").upper()
        liq = float(t.get("liquidity") or 0)
        if sym and (sym not in by or liq > by[sym]["liquidity"]):
            by[sym] = {"symbol": sym, "address": t.get("contractAddress"),
                       "decimals": int(t.get("decimals") or 18), "liquidity": liq,
                       "alpha_id": t.get("alphaId"), "name": t.get("name")}
    _alpha_cache.update(ts=time.time(), by_symbol=by)
    return by


def alpha_token(symbol: str):
    """Token Alpha en BSC para el símbolo base del perp, o None.

    Exige liquidez mínima. Símbolos con multiplicador (1000PEPE…) no se mapean.
    """
    t = _load_alpha().get((symbol or "").upper())
    if not t or not t.get("address") or t["liquidity"] < MIN_LIQUIDITY_USD:
        return None
    return dict(t)


def wallet_snapshot(address: str, symbols: list) -> dict:
    out = {"address": address, "usdt": from_wei(erc20_balance(USDT, address), USDT_DECIMALS),
           "bnb": from_wei(bnb_balance(address), 18), "tokens": {}}
    for s in symbols or []:
        t = alpha_token(s)
        if t:
            out["tokens"][s] = from_wei(erc20_balance(t["address"], address), t["decimals"])
    return out


# ── Firma / envío ───────────────────────────────────────────────────────────
def _sign_and_send(wallet: dict, tx: dict, aggregator=None) -> str:
    """Firma localmente (EIP-155) y transmite. Devuelve tx hash.

    Tipo 2 si el agregador da maxPriorityFeePerGas (maxFeePerGas = gasPrice);
    legacy con gasPrice si no. Broadcast vía agregador (MEV protection) con
    fallback a eth_sendRawTransaction.
    """
    from eth_account import Account
    from eth_utils import to_checksum_address
    acct = Account.from_key(wallet["private_key"])
    full = {
        "chainId": CHAIN_ID,
        "nonce": _nonce(acct.address),
        # eth-account exige checksum EIP-55; la lista Alpha trae direcciones en minúsculas.
        "to": to_checksum_address(tx["to"]),
        "data": tx.get("data", "0x"),
        "value": int(tx.get("value") or 0),
    }
    gas_price = int(tx.get("gasPrice") or _gas_price())
    if tx.get("maxPriorityFeePerGas"):
        full.update(type=2, maxFeePerGas=gas_price, maxPriorityFeePerGas=int(tx["maxPriorityFeePerGas"]))
    else:
        full["gasPrice"] = gas_price
    full["gas"] = _gas_limit(acct.address, full, tx.get("gas"))
    signed = acct.sign_transaction(full)
    raw = signed.raw_transaction.hex()
    raw = raw if raw.startswith("0x") else "0x" + raw
    if aggregator is not None and hasattr(aggregator, "broadcast"):
        try:
            return aggregator.broadcast(raw, acct.address)
        except Exception as e:
            log.warning(f"aggregator broadcast failed, fallback RPC: {e}")
    return _rpc("eth_sendRawTransaction", [raw])


def _approve_data(spender: str, amount: int) -> str:
    return _SEL_APPROVE + _addr_arg(spender) + hex(amount)[2:].rjust(64, "0")


# ── Pierna spot on-chain ────────────────────────────────────────────────────
class BscSpotLeg:
    """Pierna spot en BSC con la misma interfaz que trade_executor.CexSpotLeg.

    trade(side, target_base, on_fill, window, taker_ok) ejecuta un swap
    atómico (sin maker) y llama on_fill(tx_hash, fills, net_base).
    """

    venue = "bsc"
    exit_max_impact = MAX_PRICE_IMPACT_EXIT

    def __init__(self, wallet: dict, token: dict, ctx, perp_cli, perp_sym,
                 max_slippage: float = DEFAULT_SLIPPAGE, aggregator=None):
        if not token or not token.get("address"):
            raise ValueError("token Alpha BSC no disponible")
        self.wallet = wallet
        self.address = wallet["address"]
        self.token = token
        self.ctx = ctx
        self.perp_cli = perp_cli
        self.perp_sym = perp_sym
        self.max_slippage = max_slippage
        self.max_impact = MAX_PRICE_IMPACT
        self.agg = aggregator or get_aggregator()
        self.order_ids = []   # tx hashes
        self.fee_usd = 0.0

    def held_base(self) -> float:
        return from_wei(erc20_balance(self.token["address"], self.address), self.token["decimals"])

    def _bnb_usd(self) -> float:
        try:
            t = self.perp_cli.fetch_ticker("BNB/USDT:USDT")
            return float(t.get("last") or 0)
        except Exception:
            return 0.0

    def _ensure_allowance(self, token_addr, amount_wei, spender):
        if erc20_allowance(token_addr, self.address, spender) >= amount_wei:
            return
        txh = _sign_and_send(self.wallet, {"to": token_addr, "data": _approve_data(spender, amount_wei)},
                             self.agg)
        rc = wait_receipt(txh)
        self._gas_fee(rc)
        if int(rc.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"approve revertido ({txh})")

    def _gas_fee(self, rc):
        try:
            used = int(rc.get("gasUsed", "0x0"), 16)
            price = int(rc.get("effectiveGasPrice", "0x0"), 16)
            self.fee_usd += from_wei(used * price, 18) * self._bnb_usd()
        except Exception:
            pass

    def _swap(self, src, src_dec, dst, dst_dec, amount_in: float):
        """Ejecuta un swap y devuelve (in_real, out_real, tx_hash).

        Approve (acotado al monto) ANTES del swap definitivo: el quoteId del
        agregador caduca en ~30 s, así que tras aprobar se pide un swap fresco.
        """
        amt_wei = to_wei(amount_in, src_dec)
        if src.lower() != NATIVE.lower():
            # Nunca más que el saldo real (vender "todo" llega como float redondeado).
            amt_wei = min(amt_wei, erc20_balance(src, self.address))
        if amt_wei <= 0:
            raise RuntimeError("saldo insuficiente para el swap")
        q = self.agg.swap_tx(src, dst, amt_wei, self.address, self.max_slippage, self.max_impact)
        if src.lower() != NATIVE.lower() and \
                erc20_allowance(src, self.address, q["spender"]) < amt_wei:
            self._ensure_allowance(src, amt_wei, q["spender"])
            q = self.agg.swap_tx(src, dst, amt_wei, self.address, self.max_slippage, self.max_impact)
        impact = self._impact_vs_mid(src, src_dec, dst, dst_dec, amt_wei, q)
        if impact is None:
            impact = q.get("price_impact")
        if impact is not None and impact > self.max_impact:
            raise RuntimeError(f"impacto de precio {impact * 100:.2f}% > máx {self.max_impact * 100:.1f}%")
        before_in = erc20_balance(src, self.address)
        before_out = erc20_balance(dst, self.address)
        txh = _sign_and_send(self.wallet, q["tx"], self.agg)
        self.order_ids.append(txh)
        rc = wait_receipt(txh)
        self._gas_fee(rc)
        if int(rc.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"swap revertido ({txh})")
        spent = from_wei(before_in - erc20_balance(src, self.address), src_dec)
        got = from_wei(erc20_balance(dst, self.address) - before_out, dst_dec)
        return spent, got, txh

    def _impact_vs_mid(self, src, src_dec, dst, dst_dec, amt_wei, q):
        """Impacto real: precio efectivo del swap vs mid on-chain (cotizaciones chicas).
        El price_impact de Kyber sale de su oráculo USD y en un pico se separa del pool
        (AGT 2026-10-23: 9.86 % reportado, ~0 % real). None si no se pudo medir."""
        selling = dst.lower() == USDT.lower()
        if not selling and src.lower() != USDT.lower():
            return None
        try:
            out = q.get("to_amount") or self.agg.quote(src, dst, amt_wei, self.address)["to_amount"]
            a_in, a_out = from_wei(amt_wei, src_dec), from_wei(int(out), dst_dec)
            if a_in <= 0 or a_out <= 0:
                return None
            px = a_out / a_in if selling else a_in / a_out   # USDT por token
            mid = self.mid_price(px)
            if not mid:
                return None
            return max(0.0, 1 - px / mid) if selling else max(0.0, px / mid - 1)
        except Exception as e:
            log.warning(f"impacto vs mid {self.token.get('symbol')}: {e}")
            return None

    def quote_price(self, base_amt: float, side: str, ref_px: float = 0.0) -> float:
        """Precio medio estimado (USDT por token) para comprar/vender base_amt."""
        dec = self.token["decimals"]
        if not base_amt:
            return 0.0
        if side == "buy":
            usdt = base_amt * (ref_px or self.quote_price(base_amt, "sell"))
            q = self.agg.quote(USDT, self.token["address"], to_wei(usdt, USDT_DECIMALS), self.address)
            got = from_wei(int(q["to_amount"]), dec)
            return usdt / got if got else 0.0
        q = self.agg.quote(self.token["address"], USDT, to_wei(base_amt, dec), self.address)
        return from_wei(int(q["to_amount"]), USDT_DECIMALS) / base_amt

    def mid_price(self, ref_px: float, usd: float = 25.0):
        """Precio justo on-chain: mid de cotizaciones chicas de compra y venta
        (la comisión del pool se cancela; sin impacto). Referencia de slippage."""
        if not ref_px:
            return None
        base = usd / ref_px
        buy = self.quote_price(base, "buy", ref_px=ref_px)
        sell = self.quote_price(base, "sell")
        return (buy + sell) / 2 if buy and sell else (buy or sell or None)

    def trade(self, side, target_base, on_fill, window, taker_ok):
        if target_base <= 0 or self.ctx.cancelled():
            return 0.0
        tok, dec = self.token["address"], self.token["decimals"]
        if side == "buy":
            try:
                pb = float(self.perp_cli.fetch_ticker(self.perp_sym).get("bid") or 0)
            except Exception:
                pb = 0
            px = self.quote_price(target_base, "buy", ref_px=pb)
            if px and pb and px > pb * (1 + ONCHAIN_MAX_BASIS):
                log.info(f"on-chain {self.token['symbol']} px {px:.6g} > perp bid {pb:.6g}·(1+{ONCHAIN_MAX_BASIS}) — no compra")
                return 0.0
            usdt_in = target_base * px * (1 + self.max_slippage)
            have = from_wei(erc20_balance(USDT, self.address), USDT_DECIMALS)
            usdt_in = min(usdt_in, have)
            if usdt_in <= 0:
                raise RuntimeError("Sin USDT en la wallet BSC")
            spent, got, txh = self._swap(USDT, USDT_DECIMALS, tok, dec, usdt_in)
            on_fill(txh, {"gross": got, "cost": spent, "avg": spent / got if got else 0,
                          "fee_usd": 0.0, "fee_base": 0.0}, got)
            return got
        amt = min(target_base, self.held_base())
        if amt <= 0:
            return 0.0
        spent, got, txh = self._swap(tok, dec, USDT, USDT_DECIMALS, amt)
        on_fill(txh, {"gross": spent, "cost": got, "avg": got / spent if spent else 0,
                      "fee_usd": 0.0, "fee_base": 0.0}, spent)
        return spent

    def unwind(self, base_amt) -> bool:
        amt = min(base_amt, self.held_base())
        if amt <= 0:
            return True
        self._swap(self.token["address"], self.token["decimals"], USDT, USDT_DECIMALS, amt)
        return True


# ── Agregador DEX ───────────────────────────────────────────────────────────
_AGG = None


def get_aggregator():
    """Agregador configurado (Binance Web3 API por defecto)."""
    global _AGG
    if _AGG is None:
        from exchanges.dex_aggregator import build_aggregator
        _AGG = build_aggregator()
    return _AGG
