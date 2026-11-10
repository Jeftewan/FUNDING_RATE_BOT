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
import math
import os
import time

import requests

from exchanges.net import requests_proxies

log = logging.getLogger("bot.bsc")

CHAIN_ID = 56
USDT = "0x55d398326f99059fF775485246999027B3197955"  # BSC-USD (Binance-Peg USDT), 18 dec
USDT_DECIMALS = 18
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
DEFAULT_SLIPPAGE = 0.002          # 0.2%: la tolerancia es lo que un sándwich puede extraer (ZEST #52)
NAKED_RETRY_SLIPPAGE = 0.005      # reintento de un tramo revertido del spot desnudo
SWAP_RETRIES = 2                  # recotizaciones tras un swap revertido (el perp aún no se tocó)
SWAP_RETRY_WAIT = 3
PRIVATE_RPC_RETRIES = 3
SLICE_IMPACT = 0.0015             # troceo normal: cada tramo mueve ≤ 0.15 % vs mid
SLICE_MAX = 6
SLICE_GAP = 45                    # s entre tramos: el pool se re-arbitra
LIMIT_THETA = 0.002               # orden límite: precio a 0.2 % a favor del perp
LIMIT_CROSS = 0.001               # nunca paga más de 0.1 % sobre el mid on-chain
LIMIT_WINDOW = 600                # s por intento límite; expira sola en Kyber al mismo plazo
LIMIT_POLL = 3
LIMIT_REPEG = 60
LIMIT_REPEG_MOVE = 0.001
LIMIT_CANCEL_WAIT = 90
LIMIT_SHORT_CHECK = 15
LIMIT_MAX_USD = 3000              # tope del piloto por orden
MAX_PRICE_IMPACT = 0.01           # 1%
PERP_ROUND_TRIP_FEE = 0.10        # %: perp taker a la entrada y a la salida
MAX_PRICE_IMPACT_EXIT = 0.05      # vender spot desnudo (short ya cerrado): peor es quedarse sin cobertura
# Spot desnudo (AIN #51, 2026-10-31): un solo swap de 1690 tokens salió 1.46 % bajo el
# mid, con sándwich. Se trocea si el total mueve > EXIT_SPLIT_IMPACT y cada tramo va con
# slippage corto (menos botín para el sándwich); un tramo revertido reintenta con el normal.
EXIT_SPLIT_IMPACT = 0.005
EXIT_MAX_SLICES = 4
EXIT_SLICE_GAP = 10               # s entre tramos: el pool se re-arbitra
EXIT_SLIPPAGE = 0.003
ONCHAIN_MAX_BASIS = 0.005         # compra on-chain hasta 0.5% sobre el perp bid (incluye fee del DEX)
MIN_LIQUIDITY_USD = 200_000       # liquidez mínima del token (lista Alpha)
RECEIPT_TIMEOUT = 90
GAS_BUFFER = 1.3                  # margen sobre eth_estimateGas (el estado cambia entre estimar y minar)
ALPHA_LIST_URL = ("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/"
                  "wallet/cex/alpha/all/token/list")
_ALPHA_TTL = 3600
_alpha_cache = {"ts": 0.0, "by_symbol": {}, "any_by_symbol": {}}

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
def _rpc(method: str, params: list, url: str = None):
    r = requests.post(url or rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method,
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
    by, any_by = {}, {}
    for t in rows:
        if str(t.get("chainId")) != str(CHAIN_ID):
            continue
        sym = (t.get("symbol") or "").upper()
        liq = float(t.get("liquidity") or 0)
        row = {"symbol": sym, "address": t.get("contractAddress"),
               "decimals": int(t.get("decimals") or 18), "liquidity": liq,
               "alpha_id": t.get("alphaId"), "name": t.get("name")}
        if sym and (sym not in any_by or liq > any_by[sym]["liquidity"]):
            any_by[sym] = row
        if t.get("offline") or t.get("fullyDelisted"):
            continue
        if sym and (sym not in by or liq > by[sym]["liquidity"]):
            by[sym] = row
    _alpha_cache.update(ts=time.time(), by_symbol=by, any_by_symbol=any_by)
    return by


def alpha_token(symbol: str, for_exit: bool = False):
    """Token Alpha en BSC para el símbolo base del perp, o None.

    Exige liquidez mínima. Símbolos con multiplicador (1000PEPE…) no se mapean.
    `for_exit`: para vender/vigilar lo que ya se tiene. Acepta tokens que Binance pasó
    a offline (el contrato y el pool siguen) y sin mínimo de liquidez: ARIA #56 quedó
    offline con la posición abierta y el cierre tras el SL abortó sin token.
    """
    by = _load_alpha()
    if for_exit:
        t = _alpha_cache["any_by_symbol"].get((symbol or "").upper())
        return dict(t) if t and t.get("address") else None
    t = by.get((symbol or "").upper())
    if not t or not t.get("address") or t["liquidity"] < MIN_LIQUIDITY_USD:
        return None
    return dict(t)


def wallet_snapshot(address: str, symbols: list) -> dict:
    out = {"address": address, "usdt": from_wei(erc20_balance(USDT, address), USDT_DECIMALS),
           "bnb": from_wei(bnb_balance(address), 18), "tokens": {}}
    for s in symbols or []:
        t = alpha_token(s, for_exit=True)
        if t:
            out["tokens"][s] = from_wei(erc20_balance(t["address"], address), t["decimals"])
    return out


# ── Firma / envío ───────────────────────────────────────────────────────────
class SwapReverted(RuntimeError):
    pass


class BroadcastError(RuntimeError):
    pass


class ShortGone(RuntimeError):
    """El short desapareció mientras una venta límite de cierre estaba viva."""


def _sign_and_send(wallet: dict, tx: dict, aggregator=None, allow_public: bool = True,
                   meta: dict = None) -> str:
    """Firma localmente (EIP-155) y transmite. Devuelve tx hash.

    Tipo 2 si el agregador da maxPriorityFeePerGas (maxFeePerGas = gasPrice);
    legacy con gasPrice si no. Broadcast vía agregador (MEV protection) o RPC privado.
    allow_public=False: un swap no urgente nunca cae a la mempool pública (ahí lo
    sandwichean: ZEST #52, TAC #49, AIN #51); si no sale protegido, aborta.
    meta["via"] registra el canal usado.
    """
    meta = meta if meta is not None else {}
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
            h = aggregator.broadcast(raw, acct.address)
            meta["via"] = "aggregator"
            return h
        except Exception as e:
            log.warning(f"aggregator broadcast failed, fallback RPC: {e}")
    # Sin broadcast protegido del agregador (Kyber, o Web3 API bloqueada por región) la tx
    # iría a la mempool pública: RPC privado anti-MEV si está configurado.
    private = os.environ.get("BSC_PRIVATE_RPC_URL", "")
    tx_hash = signed.hash.hex()
    tx_hash = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
    if private:
        for attempt in range(PRIVATE_RPC_RETRIES):
            try:
                h = _rpc("eth_sendRawTransaction", [raw], url=private)
                meta["via"] = "private_rpc"
                return h
            except Exception as e:
                if "already known" in str(e).lower():
                    meta["via"] = "private_rpc"
                    return tx_hash
                log.warning(f"private RPC broadcast intento {attempt + 1}: {e}")
                time.sleep(1)
    if allow_public:
        meta["via"] = "public_rpc"
        return _rpc("eth_sendRawTransaction", [raw])
    if not private:
        raise BroadcastError("Sin canal protegido: configura BSC_PRIVATE_RPC_URL; swap no enviado")
    # El RPC privado pudo aceptarla aunque respondiera con error: si se minó, cuenta.
    for _ in range(3):
        time.sleep(3)
        try:
            if _rpc("eth_getTransactionReceipt", [tx_hash]):
                meta["via"] = "private_rpc"
                return tx_hash
        except Exception:
            pass
    raise BroadcastError("RPC privado no disponible; swap no enviado para evitar la mempool pública")


def _approve_data(spender: str, amount: int) -> str:
    return _SEL_APPROVE + _addr_arg(spender) + hex(amount)[2:].rjust(64, "0")


# ── Pierna spot on-chain ────────────────────────────────────────────────────
class BscSpotLeg:
    """Pierna spot en BSC con la misma interfaz que trade_executor.CexSpotLeg.

    trade(side, target_base, on_fill, window, taker_ok) ejecuta un swap
    atómico (sin maker) y llama on_fill(tx_hash, fills, net_base).
    """

    venue = "bsc"
    entry_mode = "onchain_swap"

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
        self.naked = False
        self.exit_log = []    # tramos de la venta del spot desnudo (auditoría)
        self.swaps = []       # cada swap: cotizado, mínimo, impacto y canal (auditoría)
        self._last_impact = None
        self._perp_ref = None  # perp bid/ask justo antes del swap: basis por tramo (auditoría)

    def naked_exit(self):
        """El SL (o una liquidación) ya cerró el short: vender pesa más que el precio
        (impacto hasta MAX_PRICE_IMPACT_EXIT), pero troceado y con slippage corto."""
        self.naked = True
        self.max_impact = MAX_PRICE_IMPACT_EXIT
        self.max_slippage = EXIT_SLIPPAGE

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

    def _swap_retry(self, src, src_dec, dst, dst_dec, amount_in: float):
        """Swap normal con tolerancia corta: si revierte, recotiza y reintenta. Agotados
        los intentos aborta; el perp se cubre después del swap, así que no hay exposición."""
        for attempt in range(SWAP_RETRIES + 1):
            try:
                return self._swap(src, src_dec, dst, dst_dec, amount_in)
            except SwapReverted as e:
                if attempt == SWAP_RETRIES or self.ctx.cancelled():
                    raise
                log.warning(f"swap {self.token.get('symbol')} revertido, recotizando: {e}")
                time.sleep(SWAP_RETRY_WAIT)

    def _swap(self, src, src_dec, dst, dst_dec, amount_in: float, allow_public: bool = None):
        """Ejecuta un swap y devuelve (in_real, out_real, tx_hash).

        Approve (acotado al monto) ANTES del swap definitivo: el quoteId del
        agregador caduca en ~30 s, así que tras aprobar se pide un swap fresco.
        """
        allow_public = self.naked if allow_public is None else allow_public
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
        self._last_impact = impact
        before_in = erc20_balance(src, self.address)
        before_out = erc20_balance(dst, self.address)
        meta = {}
        txh = _sign_and_send(self.wallet, q["tx"], self.agg, allow_public=allow_public, meta=meta)
        self.order_ids.append(txh)
        rc = wait_receipt(txh)
        self._gas_fee(rc)
        if int(rc.get("status", "0x0"), 16) != 1:
            raise SwapReverted(f"swap revertido ({txh})")
        spent = from_wei(before_in - erc20_balance(src, self.address), src_dec)
        got = from_wei(erc20_balance(dst, self.address) - before_out, dst_dec)
        self.swaps.append({"tx": txh, "in": spent, "out": got, "impact": impact, "via": meta.get("via"),
                           "quoted_out": from_wei(int(q["to_amount"]), dst_dec) if q.get("to_amount") else None,
                           "min_out": from_wei(int(q["min_out"]), dst_dec) if q.get("min_out") else None,
                           "slippage": self.max_slippage, "perp_ref": self._perp_ref, "ts": time.time()})
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
            self._perp_ref = pb or None
            spent, got, txh = self._swap_retry(USDT, USDT_DECIMALS, tok, dec, usdt_in)
            on_fill(txh, {"gross": got, "cost": spent, "avg": spent / got if got else 0,
                          "fee_usd": 0.0, "fee_base": 0.0}, got)
            return got
        amt = min(target_base, self.held_base())
        if amt <= 0:
            return 0.0
        if not self.naked:
            try:
                self._perp_ref = float(self.perp_cli.fetch_ticker(self.perp_sym).get("ask") or 0) or None
            except Exception:
                self._perp_ref = None
            spent, got, txh = self._swap_retry(tok, dec, USDT, USDT_DECIMALS, amt)
            on_fill(txh, {"gross": spent, "cost": got, "avg": got / spent if spent else 0,
                          "fee_usd": 0.0, "fee_base": 0.0}, spent)
            return spent
        n = self._exit_slices(amt)
        sold = 0.0
        for i in range(n):
            if i:
                if self.ctx.cancelled():
                    break
                time.sleep(EXIT_SLICE_GAP)
            part = amt - sold if i == n - 1 else amt / n
            try:
                spent, got, txh = self._swap(tok, dec, USDT, USDT_DECIMALS, part)
            except RuntimeError as e:
                if "revertido" not in str(e) or self.max_slippage >= NAKED_RETRY_SLIPPAGE:
                    raise
                log.warning(f"tramo {i + 1}/{n} {self.token['symbol']} revertido con slippage "
                            f"{self.max_slippage:.1%}; reintento con {NAKED_RETRY_SLIPPAGE:.1%}")
                self.max_slippage = NAKED_RETRY_SLIPPAGE
                spent, got, txh = self._swap(tok, dec, USDT, USDT_DECIMALS, part)
            self.exit_log.append({"slice": i + 1, "of": n, "qty": spent, "usdt": got,
                                  "px": got / spent if spent else None, "tx": txh,
                                  "impact_quoted": self._last_impact, "slippage": self.max_slippage})
            on_fill(txh, {"gross": spent, "cost": got, "avg": got / spent if spent else 0,
                          "fee_usd": 0.0, "fee_base": 0.0}, spent)
            sold += spent
        return sold

    def _exit_slices(self, amt: float) -> int:
        """Tramos para vender `amt` desnudo: ceil(impacto del total vs mid / EXIT_SPLIT_IMPACT),
        entre 1 y EXIT_MAX_SLICES. Sin medición → 1 (no demorar la salida)."""
        imp = self._size_impact(amt, "sell")
        if not imp:
            return 1
        return max(1, min(EXIT_MAX_SLICES, math.ceil(imp / EXIT_SPLIT_IMPACT)))

    def _size_impact(self, base_amt: float, side: str, ref_px: float = 0.0):
        """Impacto (fracción) de operar base_amt completo vs mid. None si no se pudo medir."""
        tok, dec = self.token["address"], self.token["decimals"]
        try:
            if side == "sell":
                src, src_dec, dst, dst_dec, amt_wei = tok, dec, USDT, USDT_DECIMALS, to_wei(base_amt, dec)
            else:
                src, src_dec, dst, dst_dec = USDT, USDT_DECIMALS, tok, dec
                amt_wei = to_wei(base_amt * ref_px, USDT_DECIMALS)
            q = self.agg.quote(src, dst, amt_wei, self.address)
            return self._impact_vs_mid(src, src_dec, dst, dst_dec, amt_wei, q)
        except Exception as e:
            log.warning(f"impacto {side} {self.token.get('symbol')}: {e}")
            return None

    def suggest_slices(self, base_amt: float, side: str, ref_px: float = 0.0) -> dict:
        """TWAP para que cada tramo mueva ≤ SLICE_IMPACT. Sin medición → sin trocear."""
        imp = self._size_impact(base_amt, side, ref_px)
        n = max(1, min(SLICE_MAX, math.ceil(imp / SLICE_IMPACT))) if imp else 1
        return {"slices": n, "duration_s": n * SLICE_GAP if n > 1 else 0,
                "suggest": n > 1, "impact_pct": round(imp * 100, 4) if imp is not None else None}

    def cost_estimate(self, base_amt: float, ref_px: float) -> dict:
        """Costo ida y vuelta a tamaño completo: impacto de compra y venta + perp taker."""
        buy, sell = self._size_impact(base_amt, "buy", ref_px), self._size_impact(base_amt, "sell")
        if buy is None or sell is None:
            return {"rt_cost_pct": None, "impact_buy_pct": None, "impact_sell_pct": None}
        return {"impact_buy_pct": round(buy * 100, 4), "impact_sell_pct": round(sell * 100, 4),
                "rt_cost_pct": round((buy + sell) * 100 + PERP_ROUND_TRIP_FEE, 4)}

    def unwind(self, base_amt) -> bool:
        amt = min(base_amt, self.held_base())
        if amt <= 0:
            return True
        # Spot sin cobertura: la urgencia pesa más que la mempool.
        self._swap(self.token["address"], self.token["decimals"], USDT, USDT_DECIMALS, amt,
                   allow_public=True)
        return True


class BscLimitLeg(BscSpotLeg):
    """Spot on-chain por orden límite Kyber (piloto), con el perp a mercado por cada llenado.

    La compra límite se llena cuando el precio baja y una venta límite del perp cuando sube:
    no son atómicas, así que solo la pierna on-chain queda en espera y el perp se cubre con
    cada llenado (PerpHedger, acumulado). El saldo del token es la fuente de verdad del fill.
    """

    entry_mode = "onchain_limit"
    keep_perp_sl = True

    def __init__(self, *args, theta: float = LIMIT_THETA, limit_window: float = LIMIT_WINDOW, **kw):
        super().__init__(*args, **kw)
        self.theta = theta
        self.limit_window = limit_window
        self.unfilled = False
        self.limit_log = []

    def _limit_price(self, side: str):
        t = self.perp_cli.fetch_ticker(self.perp_sym)
        bid, ask = float(t.get("bid") or 0), float(t.get("ask") or 0)
        ref = bid if side == "buy" else ask
        if not ref:
            return None
        mid = self.mid_price(ref)
        if side == "buy":
            px = ref * (1 - self.theta)
            return min(px, mid * (1 + LIMIT_CROSS)) if mid else px
        px = ref * (1 + self.theta)
        return max(px, mid * (1 - LIMIT_CROSS)) if mid else px

    def _token_wei(self) -> int:
        return erc20_balance(self.token["address"], self.address)

    def _save_state(self, state: dict):
        self.ctx.progress({"phase": "limit", "limit": dict(state), "_force": True})

    def _short_gone(self) -> bool:
        from exchanges.trade_executor import _open_perp_size
        size = _open_perp_size(self.perp_cli, self.perp_sym, "short")
        return size is not None and size <= 0

    def trade(self, side, target_base, on_fill, window, taker_ok):
        from exchanges import kyber_limit_order as klo
        if target_base <= 0 or self.ctx.cancelled():
            return 0.0
        start_wei = self._token_wei()
        state = {"side": side, "token": self.token, "start_wei": str(start_wei), "prior_base": 0.0,
                 "prior_cost": 0.0, "order_id": None, "order_px": None}
        deadline = time.time() + self.limit_window
        filled = 0.0
        cost = 0.0
        while filled < target_base * 0.999 and time.time() < deadline and not self.ctx.cancelled():
            px = self._limit_price(side)
            if not px:
                time.sleep(LIMIT_POLL)
                continue
            try:
                ttl = int(max(30, deadline - time.time()))
                order = klo.place_buy(self.wallet, self.token, target_base - filled, px, ttl=ttl) \
                    if side == "buy" else klo.place_sell(self.wallet, self.token, target_base - filled, px, ttl=ttl)
            except Exception as e:
                if filled > 0:
                    log.warning(f"límite {self.token['symbol']}: no se pudo re-colocar: {e}")
                    break
                log.warning(f"límite {self.token['symbol']}: Kyber LO no disponible ({e}); swap")
                return super().trade(side, target_base, on_fill, window, taker_ok)
            state.update(order_id=order["id"], order_px=px)
            self._save_state(state)
            self.order_ids.append(f"lo:{order['id']}")
            got = self._watch(order["id"], side, px, start_wei, filled, cost, deadline, on_fill, state)
            filled, cost = got
            state.update(prior_base=filled, prior_cost=cost, order_id=None, order_px=None)
            self._save_state(state)
        self.unfilled = filled <= 0
        self.limit_log.append({"side": side, "filled": filled, "cost": cost, "target": target_base,
                               "avg": cost / filled if filled else None})
        return filled

    def _filled_now(self, side, start_wei) -> float:
        dec = self.token["decimals"]
        delta = self._token_wei() - start_wei if side == "buy" else start_wei - self._token_wei()
        return from_wei(max(0, delta), dec)

    def _watch(self, oid, side, px, start_wei, prior, prior_cost, deadline, on_fill, state):
        """Vigila una orden hasta llenarse o re-anclar; siempre la deja cerrada. (filled, cost)."""
        from exchanges import kyber_limit_order as klo
        placed_at = last_short = time.time()
        reported = prior

        def report():
            nonlocal reported
            total = self._filled_now(side, start_wei)
            if total > reported + 1e-12:
                q = total - prior
                c = q * px
                on_fill(f"lo:{oid}", {"gross": q, "cost": c, "avg": px, "fee_usd": 0.0, "fee_base": 0.0}, q)
                reported = total
            return total

        try:
            while time.time() < deadline and not self.ctx.cancelled():
                report()
                if klo.order_is_final(self.wallet["address"], oid):
                    break
                if side == "sell" and time.time() - last_short >= LIMIT_SHORT_CHECK:
                    last_short = time.time()
                    if self._short_gone():
                        raise ShortGone("El SL cerró el short durante la venta límite")
                if time.time() - placed_at >= LIMIT_REPEG:
                    new_px = self._limit_price(side)
                    if new_px and abs(new_px / px - 1) >= LIMIT_REPEG_MOVE:
                        break
                    placed_at = time.time()
                time.sleep(LIMIT_POLL)
        finally:
            try:
                klo.finalize(self.wallet, oid, LIMIT_CANCEL_WAIT, LIMIT_POLL)
            except Exception as e:
                log.error(f"límite {self.token['symbol']} orden {oid}: no se pudo cerrar: {e}")
            report()
        return reported, prior_cost + (reported - prior) * px


# ── Agregador DEX ───────────────────────────────────────────────────────────
_AGG = None


def get_aggregator():
    """Agregador configurado (Binance Web3 API por defecto)."""
    global _AGG
    if _AGG is None:
        from exchanges.dex_aggregator import build_aggregator
        _AGG = build_aggregator()
    return _AGG
