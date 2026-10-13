"""Tests del motor de ejecución pareado con un exchange CCXT simulado (sin red)."""
import math

import pytest

from exchanges import trade_executor as te


class FakeEx:
    """Exchange mínimo: spot BTC/USDT + perp BTC/USDT:USDT, libro fijo.

    Las órdenes limit (maker) se llenan `fill_per_poll` por cada fetch_order;
    las market se llenan completas al instante. Fees: spot en base (0.1%),
    perp en USDT (0.05%).
    """

    def __init__(self, fill_per_poll=0.4, spot_fee_ccy="BTC"):
        self.id = "binance"
        self.has = {"fetchPositionMode": True}
        self.fill_per_poll = fill_per_poll
        self.spot_fee_ccy = spot_fee_ccy
        self.orders = {}
        self.trades = []
        self.n = 0
        self.short = 0.0
        self.spot_bal = {"BTC": 0.0, "USDT": 1e6}
        self.book = {"BTC/USDT": (100.0, 100.1), "BTC/USDT:USDT": (100.2, 100.3),
                     "BNB/USDT": (600.0, 600.0)}
        self.markets = {
            "BTC/USDT": {"id": "BTCUSDT", "base": "BTC", "spot": True, "contract": False,
                         "contractSize": None, "precision": {"amount": 0.001},
                         "limits": {"amount": {"min": 0.001}, "cost": {"min": 5}}},
            "BTC/USDT:USDT": {"id": "BTCUSDT", "base": "BTC", "spot": False, "contract": True,
                              "contractSize": 1, "precision": {"amount": 0.01},
                              "limits": {"amount": {"min": 0.01}, "cost": {"min": 5}}},
        }

    # mercado
    def market(self, s):
        return self.markets[s]

    def amount_to_precision(self, s, a):
        step = self.markets[s]["precision"]["amount"]
        return str(math.floor(a / step + 1e-9) * step)

    def price_to_precision(self, s, p):
        return str(round(p, 2))

    def fetch_ticker(self, s):
        b, a = self.book[s]
        return {"bid": b, "ask": a, "last": (a + b) / 2}

    def fetch_order_book(self, s, limit=100):
        b, a = self.book[s]
        return {"bids": [[b, 50]], "asks": [[a, 50]]}

    # órdenes
    def create_order(self, s, typ, side, amount, price=None, params=None):
        self.n += 1
        oid = str(self.n)
        o = {"id": oid, "symbol": s, "type": typ, "side": side, "amount": float(amount),
             "price": price, "filled": 0.0, "status": "open",
             "clientOrderId": (params or {}).get("clientOrderId")}
        self.orders[oid] = o
        if typ == "market":
            self._fill(o, o["amount"])
        return dict(o)

    def _fill(self, o, qty):
        qty = min(qty, o["amount"] - o["filled"])
        if qty <= 0:
            return
        b, a = self.book[o["symbol"]]
        px = o["price"] or (a if o["side"] == "buy" else b)
        o["filled"] += qty
        if o["filled"] >= o["amount"] - 1e-12:
            o["status"] = "closed"
        spot = not self.markets[o["symbol"]]["contract"]
        if spot:
            fee_ccy = self.spot_fee_ccy if o["side"] == "buy" else "USDT"
            fee = qty * 0.001 if fee_ccy == "BTC" else (qty * px * 0.001 if fee_ccy == "USDT" else qty * px * 0.00075 / 600)
            if o["side"] == "buy":
                self.spot_bal["BTC"] += qty - (fee if fee_ccy == "BTC" else 0)
            else:
                self.spot_bal["BTC"] -= qty
        else:
            fee_ccy, fee = "USDT", qty * px * 0.0005
            self.short += qty if o["side"] == "sell" else -qty
        self.trades.append({"order": o["id"], "amount": qty, "price": px, "cost": qty * px,
                            "fee": {"cost": fee, "currency": fee_ccy}})

    def fetch_order(self, oid, s):
        o = self.orders[oid]
        if o["type"] == "limit" and o["status"] == "open":
            self._fill(o, o["amount"] * self.fill_per_poll)
        return dict(o)

    def cancel_order(self, oid, s):
        o = self.orders[oid]
        if o["status"] == "open":
            o["status"] = "canceled"

    def fetch_my_trades(self, s, params=None):
        oid = (params or {}).get("orderId")
        return [dict(t) for t in self.trades if t["order"] == oid]

    def fetch_balance(self):
        return {"free": dict(self.spot_bal), "total": dict(self.spot_bal)}

    def fetch_positions(self, syms=None):
        if self.short <= 0:
            return []
        return [{"symbol": "BTC/USDT:USDT", "contracts": self.short, "side": "short",
                 "contractSize": 1}]

    def fetch_position_mode(self, s):
        return {"hedged": False}

    # config de cuenta
    def set_margin_mode(self, *a, **k):
        pass

    def set_leverage(self, *a, **k):
        pass

    def fapiprivate_get_symbolconfig(self, p):
        return [{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": self._lev}]

    def fapiprivate_get_multiassetsmargin(self):
        return {"multiAssetsMargin": self._multi}

    def fapiprivate_get_leveragebracket(self, p):
        return [{"symbol": "BTCUSDT", "brackets": [{"initialLeverage": 20, "maintMarginRatio": 0.004}]}]

    _lev = 2
    _multi = False


@pytest.fixture
def fake(monkeypatch):
    ex = FakeEx()
    monkeypatch.setattr(te, "POLL_INTERVAL", 0)
    monkeypatch.setattr(te, "REPRICE_SECS", 0.01)
    monkeypatch.setattr(te, "SPOT_LIMIT_TIMEOUT", 0.3)
    monkeypatch.setattr(te, "_build_client", lambda *a, **k: ex)
    monkeypatch.setattr(te, "_ensure_markets", lambda c: None)
    monkeypatch.setattr(te.time, "sleep", lambda s: None)
    return ex


OPP = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "price": 100.0}
CREDS = {"binance": {"api_key": "k", "api_secret": "s"}}


def test_open_hedges_net_spot_after_base_fee(fake):
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2, ctx=te.ExecContext(job_id=7))
    assert res["ok"], res
    # spot = 300·2/3 = 200 USD → ~2 BTC al bid 100; fee 0.1% en base → neto ≈ 1.998
    assert res["spot_qty"] == pytest.approx(fake.spot_bal["BTC"], rel=1e-9)
    # El short cubre el neto truncado al paso del perp (0.01) — nunca más que el spot.
    assert res["perp_qty"] <= res["spot_qty"] + 1e-9
    assert res["spot_qty"] - res["perp_qty"] < 0.01
    assert fake.short == pytest.approx(res["perp_qty"])
    assert res["entry_fees_usd"] > 0
    # Idempotencia: todas las órdenes llevan clientOrderId bsy7-…
    assert all(o["clientOrderId"].startswith("bsy7-") for o in fake.orders.values())


def test_open_bnb_fee_does_not_reduce_hedge(fake):
    fake.spot_fee_ccy = "BNB"
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2)
    assert res["ok"], res
    assert res["spot_qty"] == pytest.approx(2.0, abs=0.01)
    assert res["perp_qty"] == pytest.approx(2.0, abs=0.01)


def test_multi_assets_blocks_before_orders(fake):
    fake._multi = True
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2)
    assert not res["ok"] and res["code"] == "MULTI_ASSETS"
    assert fake.orders == {}


def test_leverage_above_max_rejected(fake):
    res = te.execute_open(CREDS, OPP, capital=300, leverage=25)
    assert not res["ok"] and "máximo" in res["msg"]


def test_dry_run_reports_liquidation_and_places_nothing(fake):
    res = te.execute_open(CREDS, OPP, capital=300, leverage=3, dry_run=True)
    assert res["ok"] and res["dry_run"]
    assert res["liquidation"]["liq_distance_pct"] == pytest.approx((1 / 3 - 0.004) * 100, abs=0.01)
    assert fake.orders == {}


def test_negative_basis_blocks_taker_fallback(fake):
    fake.fill_per_poll = 0.0          # el maker nunca llena
    fake.book["BTC/USDT"] = (101.0, 101.1)   # spot muy por encima del perp
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2)
    assert not res["ok"]
    assert fake.short == 0 and fake.spot_bal["BTC"] == 0


def test_close_uses_persisted_qty_not_whole_balance(fake):
    fake.spot_bal["BTC"] = 5.0        # el usuario tenía BTC propios
    fake.short = 1.99
    pos = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance",
           "spot_qty": 1.998, "perp_qty": 1.99, "entry_price": 100}
    res = te.execute_close(CREDS, pos)
    assert res["ok"], res
    assert res["spot_sold"] == pytest.approx(1.998, abs=0.001)
    assert fake.spot_bal["BTC"] == pytest.approx(5.0 - res["spot_sold"])
    assert fake.short == pytest.approx(0, abs=1e-9)
    assert res["complete"]


def test_close_without_qty_refuses(fake):
    pos = {"mode": "spot_perp", "symbol": "BTC", "exchange": "Binance", "exposure": 200}
    res = te.execute_close(CREDS, pos)
    assert not res["ok"] and "cantidades" in res["msg"]


def test_twap_slices_keep_delta_neutral(fake):
    progress = []
    ctx = te.ExecContext(job_id=9, progress=progress.append)
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2, ctx=ctx,
                          twap={"slices": 4, "duration_s": 4})
    assert res["ok"], res
    # En cada evento de progreso el short nunca supera al spot neto.
    assert progress and all(p["perp_hedged"] <= p["spot_net"] + 1e-9 for p in progress)
    assert res["spot_qty"] == pytest.approx(2.0, abs=0.01)


def test_cancel_saves_what_was_executed(fake):
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 3

    ctx = te.ExecContext(job_id=1, cancelled=cancelled)
    res = te.execute_open(CREDS, OPP, capital=300, leverage=2, ctx=ctx,
                          twap={"slices": 4, "duration_s": 4})
    # Lo ejecutado queda cubierto (o nada se abrió).
    assert fake.short <= fake.spot_bal["BTC"] + 1e-9


def test_liquidation_estimate():
    r = te.liquidation_estimate(100, 2, 0.004)
    assert r["liq_price"] == pytest.approx(100 * (1 + 0.5 - 0.004))
    assert not r["warning"]
    assert te.liquidation_estimate(100, 5)["warning"]
