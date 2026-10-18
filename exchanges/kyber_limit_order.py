"""Órdenes límite on-chain (BSC) vía KyberSwap Limit Order (gasless).

Se usa para el "seguro" de la pierna spot on-chain: una venta límite del token
al precio del SL del short. La orden es off-chain firmada (EIP-712) y la llena
un taker on-chain cuando el mercado llega al precio; los tokens no salen de la
wallet hasta el fill (solo se aprueba el contrato de Kyber). Expira a los
ORDER_TTL (7 días) → el vigilante (execution/account_sync.py) la renueva.

Flujo: sign-message (Kyber arma el typed data con salt/feeConfig) → approve del
contrato DSLO acotado a lo activo + la nueva → firma local con eth-account →
POST /orders. Cancelación gasless (cancel-sign → cancel). Mismo X-Client-Id en
firmar y enviar (lo exige Kyber para validar feeConfig).
"""
import logging
import time

import requests

from exchanges import onchain_bsc as bsc

log = logging.getLogger("bot.kyberlo")

BASE = "https://limit-order.kyberswap.com"
CHAIN_ID = "56"
CLIENT_ID = "basyo"
ORDER_TTL = 7 * 86400
ACTIVE_STATUSES = {"open", "partially_filled", "cancelling"}


class LimitOrderError(RuntimeError):
    pass


def _req(method, path, params=None, body=None):
    r = requests.request(method, BASE + path, params=params, json=body,
                         headers={"X-Client-Id": CLIENT_ID}, timeout=15)
    try:
        j = r.json()
    except ValueError:
        raise LimitOrderError(f"Kyber LO HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400 or j.get("code") not in (0, None):
        raise LimitOrderError(f"Kyber LO {j.get('code')}: {j.get('message')}")
    return j.get("data") or {}


def _sign_typed(private_key: str, typed: dict) -> str:
    """signTypedData v4 local. Los uint llegan como string → int para eth-account."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    types = typed["types"]
    primary = typed["primaryType"]
    msg = dict(typed["message"])
    for f in types.get(primary, []):
        if f["type"].startswith("uint") and isinstance(msg.get(f["name"]), str):
            msg[f["name"]] = int(msg[f["name"]])
    domain = dict(typed["domain"])
    if isinstance(domain.get("chainId"), str):
        domain["chainId"] = int(domain["chainId"])
    full = {"types": types, "primaryType": primary, "domain": domain, "message": msg}
    sig = Account.sign_message(encode_typed_data(full_message=full), private_key).signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def active_making_amount(maker: str, token: str) -> int:
    d = _req("GET", "/read-ks/api/v1/orders/active-making-amount",
             {"chainId": CHAIN_ID, "makerAsset": token, "maker": maker})
    return int(d.get("activeMakingAmount") or 0)


def contract_address() -> str:
    return _req("GET", "/read-ks/api/v1/configs/contract-address", {"chainId": CHAIN_ID})["latest"]


def _ensure_allowance(wallet: dict, token: str, spender: str, amount: int):
    if bsc.erc20_allowance(token, wallet["address"], spender) >= amount:
        return
    txh = bsc._sign_and_send(wallet, {"to": token, "data": bsc._approve_data(spender, amount)})
    rc = bsc.wait_receipt(txh)
    if int(rc.get("status", "0x0"), 16) != 1:
        raise LimitOrderError(f"approve Kyber LO revertido ({txh})")


def place_sell(wallet: dict, token: dict, base_qty: float, price: float, ttl: int = ORDER_TTL) -> dict:
    """Venta límite de `base_qty` token a `price` USDT. Devuelve
    {id, expires_at, making, taking}."""
    maker = wallet["address"]
    dec = int(token["decimals"])
    making = min(bsc.to_wei(base_qty, dec), bsc.erc20_balance(token["address"], maker))
    taking = bsc.to_wei(bsc.from_wei(making, dec) * price, bsc.USDT_DECIMALS)
    if making <= 0 or taking <= 0:
        raise LimitOrderError("cantidad/precio inválidos para la orden límite")
    expires = int(time.time()) + int(ttl)
    body = {"chainId": CHAIN_ID, "makerAsset": token["address"], "takerAsset": bsc.USDT,
            "maker": maker, "makingAmount": str(making), "takingAmount": str(taking),
            "expiredAt": expires}
    typed = _req("POST", "/write/api/v1/orders/sign-message", body=body)
    spender = (typed.get("domain") or {}).get("verifyingContract") or contract_address()
    _ensure_allowance(wallet, token["address"], spender,
                      active_making_amount(maker, token["address"]) + making)
    sig = _sign_typed(wallet["private_key"], typed)
    d = _req("POST", "/write/api/v1/orders",
             body={**body, "salt": str(typed["message"]["salt"]), "signature": sig})
    oid = d.get("id") or d.get("orderId")
    if not oid:
        raise LimitOrderError(f"Kyber LO sin id de orden: {d}")
    return {"id": int(oid), "expires_at": expires, "making": making, "taking": taking}


def get_order(maker: str, order_id: int):
    """La orden por id (busca en activas y luego cerradas), o None."""
    for status in ("active", "closed"):
        d = _req("GET", "/read-ks/api/v1/orders",
                 {"chainId": CHAIN_ID, "maker": maker, "status": status, "pageSize": 100})
        for o in d.get("orders") or []:
            if int(o.get("id") or 0) == int(order_id):
                return o
    return None


def cancel(wallet: dict, order_ids: list):
    """Cancelación gasless. Si queda 'cancelling', un fill en vuelo aún es posible;
    al vender los tokens la orden queda sin saldo y no se puede llenar."""
    ids = [int(i) for i in order_ids if i]
    if not ids:
        return
    typed = _req("POST", "/write/api/v1/orders/cancel-sign",
                 body={"chainId": CHAIN_ID, "maker": wallet["address"], "orderIds": ids})
    sig = _sign_typed(wallet["private_key"], typed)
    _req("POST", "/write/api/v1/orders/cancel",
         body={"chainId": CHAIN_ID, "maker": wallet["address"], "orderIds": ids, "signature": sig})
