"""Pierna spot on-chain (BSC) con agregador y cadena simulados, sin red."""
import pytest

from exchanges import onchain_bsc as ob
from exchanges import trade_executor as te
from tests.test_trade_executor import FakeEx

TOKEN = {"symbol": "XYZ", "address": "0x" + "ab" * 20, "decimals": 18, "liquidity": 1e6}
WALLET = {"address": "0x" + "cd" * 20, "private_key": "0x" + "11" * 32}


class Chain:
    """Saldos ERC20 en wei + swaps a precio fijo (2 USDT por XYZ, 0.3% de fee)."""

    def __init__(self, price=2.0):
        self.price = price
        self.bal = {ob.USDT.lower(): ob.to_wei(1000, 18), TOKEN["address"].lower(): 0}
        self.allow = {}
        self.pending = None
        self.sent = []

    # agregador
    def quote(self, src, dst, amount_wei, wallet):
        return {"to_amount": self._out(src, amount_wei), "price_impact": 0.001}

    def _out(self, src, amt):
        if src.lower() == ob.USDT.lower():
            return int(amt / self.price * 0.997)
        return int(amt * self.price * 0.997)

    def swap_tx(self, src, dst, amount_wei, wallet, slippage, max_impact=0.01):
        return {"tx": {"to": "0xrouter", "data": "0xswap", "value": 0, "gas": 200000,
                       "gasPrice": 50_000_000, "_swap": (src, dst, amount_wei)},
                "min_out": 0, "spender": "0xrouter", "price_impact": 0.001}

    def broadcast(self, raw, wallet):
        raise RuntimeError("use rpc")


@pytest.fixture
def chain(monkeypatch):
    c = Chain()
    monkeypatch.setattr(ob, "erc20_balance", lambda tok, owner: c.bal.get(tok.lower(), 0))
    monkeypatch.setattr(ob, "erc20_allowance", lambda tok, o, sp: c.allow.get(tok.lower(), 0))

    def send(wallet, tx, agg=None):
        if tx.get("_swap"):
            src, dst, amt = tx["_swap"]
            c.bal[src.lower()] -= amt
            c.bal[dst.lower()] = c.bal.get(dst.lower(), 0) + c._out(src, amt)
        else:  # approve
            c.allow[tx["to"].lower()] = 10 ** 40
        c.sent.append(tx)
        return f"0xtx{len(c.sent)}"

    monkeypatch.setattr(ob, "_sign_and_send", send)
    monkeypatch.setattr(ob, "wait_receipt", lambda h, timeout=0: {
        "status": "0x1", "gasUsed": hex(150000), "effectiveGasPrice": hex(50_000_000)})
    return c


@pytest.fixture
def perp(monkeypatch):
    ex = FakeEx()
    ex.book["XYZ/USDT:USDT"] = (2.0, 2.002)
    ex.book["BNB/USDT:USDT"] = (600, 600)
    ex.markets["XYZ/USDT:USDT"] = {"id": "XYZUSDT", "base": "XYZ", "spot": False, "contract": True,
                                   "contractSize": 1, "precision": {"amount": 1},
                                   "limits": {"amount": {"min": 1}, "cost": {"min": 5}}}
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(te, "_ensure_markets", lambda c: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    return ex


def _factory(chain):
    return lambda pc, ps, ctx: ob.BscSpotLeg(WALLET, TOKEN, ctx, pc, ps, aggregator=chain)


OPP = {"mode": "spot_perp", "symbol": "XYZ", "exchange": "Binance", "price": 2.0}
CREDS = {"binance": {"api_key": "k", "api_secret": "s"}}


def test_onchain_open_hedges_received_amount(chain, perp):
    res = te.execute_open(CREDS, OPP, capital=150, leverage=2, spot_leg_factory=_factory(chain))
    assert res["ok"], res
    held = ob.from_wei(chain.bal[TOKEN["address"].lower()], 18)
    assert res["spot_qty"] == pytest.approx(held)
    # perp step = 1 → short = floor(held)
    assert res["perp_qty"] == int(held) and perp.short == int(held)
    assert res["spot_venue"] == "bsc"
    assert chain.sent[0].get("_swap") is None   # primero approve acotado, luego swap


def test_onchain_buy_blocked_when_dex_far_above_perp(chain, perp):
    chain.price = 2.2   # DEX 10% por encima del perp
    res = te.execute_open(CREDS, OPP, capital=150, leverage=2, spot_leg_factory=_factory(chain))
    assert not res["ok"]
    assert perp.short == 0


def test_onchain_close_sells_tokens_and_covers(chain, perp):
    chain.bal[TOKEN["address"].lower()] = ob.to_wei(50.3, 18)
    perp.short = 50
    pos = {"mode": "spot_perp", "symbol": "XYZ", "exchange": "Binance", "spot_venue": "bsc",
           "spot_qty": 50.3, "perp_qty": 50, "entry_price": 2.0}
    res = te.execute_close(CREDS, pos, spot_leg_factory=_factory(chain))
    assert res["ok"], res
    assert perp.short == pytest.approx(0)
    assert ob.from_wei(chain.bal[TOKEN["address"].lower()], 18) == pytest.approx(0, abs=1e-9)


def test_dry_run_quotes_onchain(chain, perp):
    res = te.execute_open(CREDS, OPP, capital=150, leverage=2, dry_run=True,
                          spot_leg_factory=_factory(chain))
    assert res["ok"] and res["spot_venue"] == "bsc"
    assert res["basis_pct"] is not None
    assert chain.sent == []


def test_key_helpers():
    assert ob.normalize_key("11" * 32) == "0x" + "11" * 32
    with pytest.raises(ValueError):
        ob.normalize_key("0x1234")


def test_gas_limit_uses_estimate_over_short_hint(monkeypatch):
    # Caso real: el agregador sugirió 250k y el swap necesitaba ~364k (out-of-gas).
    monkeypatch.setattr(ob, "_estimate_gas", lambda tx: 364_000)
    tx = {"to": "0xrouter", "data": "0xswap", "value": 0}
    assert ob._gas_limit(WALLET["address"], tx, 250_000) == int(364_000 * ob.GAS_BUFFER)
    monkeypatch.setattr(ob, "_estimate_gas", lambda tx: 100_000)
    assert ob._gas_limit(WALLET["address"], tx, 250_000) == 250_000


def test_gas_limit_aborts_if_simulation_reverts(monkeypatch):
    def boom(tx):
        raise RuntimeError("RPC eth_estimateGas: execution reverted")
    monkeypatch.setattr(ob, "_estimate_gas", boom)
    with pytest.raises(RuntimeError):
        ob._gas_limit(WALLET["address"], {"to": "0xr", "data": "0x", "value": 0}, 250_000)
