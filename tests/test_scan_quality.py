"""Calidad del scan / monitor / salidas (auditoría 2026-10-22) — sin red.

Cada test fija un defecto concreto: paridad de ventana con el entrenamiento,
universo de candidatos, net_3d, hold estimado, earnings por pago, alertas de
salida, switch, fee_loader, caché de historial y scan horario.
"""
import sys
import time
from pathlib import Path

import pytest

from analysis import arbitrage as arb
from analysis import ml_features as mf
from core.models import FundingHistory, FundingRate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from _scoring_data import base_window_features  # noqa: E402


class Cfg:
    ARBITRAGE_MODES = ["spot_perp"]
    MIN_VOLUME = 1
    DEFI_EXCHANGES = []


class FakeEM:
    """ExchangeManager mínimo: historial fijo y set de spot."""

    def __init__(self, rates, spot=("AAA", "BBB", "CCC")):
        self.rates = rates
        self.spot = set(spot)
        self.calls = []

    def fetch_funding_history(self, symbol, exchange, limit=30, use_cache=True):
        self.calls.append((symbol, limit))
        r = self.rates[-limit:]
        return FundingHistory(symbol=symbol, exchange=exchange, rates=r,
                              timestamps=list(range(len(r))))

    def fetch_spot_availability(self, symbol, exchange):
        return symbol in self.spot


def _fr(sym, rate, ih=8, vol=5e6):
    return FundingRate(symbol=sym, pair=f"{sym}USDT", exchange="Binance", rate=rate, price=1.0,
                       volume_24h=vol, interval_hours=ih, payments_per_day=24 / ih)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    from analysis import ml_scorer
    monkeypatch.setattr(ml_scorer, "predict_score", lambda *a, **k: None)
    monkeypatch.setattr(arb, "calculate_spot_perp_fees",
                        lambda ex, cap, vol, symbol=None: {"total_cost": cap * 0.003})


# ── Paridad de ventana con el entrenamiento ─────────────────────────────────
def test_prod_window_features_match_offline_for_every_interval():
    # 40 liquidaciones: la ventana del modelo son las ÚLTIMAS 30 en prod y offline.
    hist = [0.0001 * (1 + (i % 7) / 10) * (-1 if i in (3, 17) else 1) for i in range(40)]
    for ih in (1, 4, 8):
        em = FakeEM(hist)
        sc = arb.ArbitrageScanner(em, Cfg())
        fr = _fr("AAA", 0.0002, ih=ih)
        opp = sc._analyze_spot_perp(fr)
        n = len(hist)
        off = base_window_features(hist + [fr.rate], [1] * (n + 1), [ih] * (n + 1), n, mf.LOOKBACK)
        assert opp.history["streak"] == off["streak"]
        assert opp.history["pct"] == pytest.approx(off["pct"])
        assert opp.history["settlement_avg"] == pytest.approx(off["settlement_avg"])
        assert opp.history["fee_drag"] == pytest.approx(off["fee_drag"])
        assert len(opp.history["_rates"]) == mf.LOOKBACK


def test_thin_history_skips_model(monkeypatch):
    from analysis import ml_scorer
    called = []
    monkeypatch.setattr(ml_scorer, "predict_score", lambda *a, **k: called.append(1) or (90, 50.0))
    sc = arb.ArbitrageScanner(FakeEM([0.0001, 0.0001, 0.0001]), Cfg())
    opp = sc._analyze_spot_perp(_fr("AAA", 0.0002))
    assert opp.model_prediction is None and not called


# ── Universo de candidatos ──────────────────────────────────────────────────
def test_candidates_use_daily_rate_and_require_spot():
    em = FakeEM([0.0001] * 30, spot=("H1", "E8"))
    sc = arb.ArbitrageScanner(em, Cfg())
    rates = {"binance": [
        _fr("H1", 0.00002, ih=1),     # 0.048 %/día → entra (antes: 0.00002 < 0.0001 la excluía)
        _fr("E8", 0.00008, ih=8),     # 0.024 %/día → fuera
        _fr("NOSPOT", 0.001, ih=8),   # sin spot CEX ni on-chain → fuera
    ]}
    syms = {o.symbol for o in sc.scan_spot_perp_opportunities(rates)}
    assert syms == {"H1"}


# ── net_3d ──────────────────────────────────────────────────────────────────
def test_moderate_persistent_pair_is_not_dropped_by_3d_payback():
    # 0.03 %/día estable: no recupera fees en 3 días, pero es justo lo que rinde en el tiempo.
    em = FakeEM([0.0001] * 30, spot=("AAA",))
    sc = arb.ArbitrageScanner(em, Cfg())
    opps = sc.scan_spot_perp_opportunities({"binance": [_fr("AAA", 0.000101, ih=8)]})
    assert [o.symbol for o in opps] == ["AAA"] and opps[0].net_3d_revenue_per_1000 < 0


def test_net_3d_uses_perp_leg_and_keeps_sign():
    sc = arb.ArbitrageScanner(FakeEM([-0.0002] * 30), Cfg())
    opp = sc._analyze_spot_perp(_fr("AAA", 0.0001))
    # 9 pagos de −0.02 % sobre la pierna perp (500 USD) = −0.90 USD, menos fees 3 USD
    assert opp.net_3d_revenue_per_1000 == pytest.approx(500 * -0.0018 - 3.0)
    sc = arb.ArbitrageScanner(FakeEM([0.0002] * 30), Cfg())
    opp = sc._analyze_spot_perp(_fr("AAA", 0.0002))
    assert opp.net_3d_revenue_per_1000 == pytest.approx(500 * 0.0018 - 3.0)


def test_estimated_hold_days_uses_real_payments_per_day():
    from analysis.scoring import estimated_hold_days
    hist = {"streak": 30, "pct": 95, "_rates": [1] * 30}
    assert estimated_hold_days(hist, 24) == 0     # 30 pagos de 1h = 1.25 d (antes: "10 d")
    assert estimated_hold_days(hist, 3) == 7      # 30 pagos de 8h = 10 d


# ── Monitor: earnings y alertas de salida ───────────────────────────────────
class _SM:
    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self.state = {}

    def get(self, k, d=None):
        return self.state.get(k, d)


class _EMSettle:
    def __init__(self, table):
        self.table = table          # {ts: rate}
        self.calls = 0

    def fetch_settlement_rates(self, symbol, exchange, ts_list, tolerance_secs=120):
        self.calls += 1
        return {ts: self.table.get(ts) for ts in ts_list}


def _worker(em):
    from scanner.worker import ScannerWorker
    return ScannerWorker(em, None, _SM(), None, Cfg())


def test_missed_payments_recorded_one_by_one_with_their_rates():
    now = 1_800_000_000.0
    ih = 8
    last_pay = (now // (ih * 3600)) * ih * 3600
    table = {last_pay: 0.0003, last_pay - 28800: 0.0001, last_pay - 57600: -0.0002}
    w = _worker(_EMSettle(table))
    pos = {"symbol": "AAA", "exchange": "Binance", "ih": ih, "entry_time": (last_pay - 60000) * 1000,
           "last_earn_update": last_pay - 60000, "exposure": 1000, "capital_used": 2000,
           "earned_real": 0}
    cur = {"symbol": "AAA", "exchange": "Binance", "ih": ih, "fr": 0.0005,
           "next_funding_ts": int((last_pay + ih * 3600) * 1000)}
    w._update_spot_perp_earnings(pos, [cur], now)
    pays = pos["payments"]
    assert [p["rate"] for p in pays] == [-0.0002, 0.0001, 0.0003]
    assert pos["earned_real"] == pytest.approx(1000 * (0.0003 + 0.0001 - 0.0002))


def test_unpublished_settlement_is_retried_not_booked_with_predicted_rate():
    now = 1_800_000_000.0
    ih = 8
    last_pay = (now // (ih * 3600)) * ih * 3600
    now = last_pay + 60                         # 1 min después del settlement
    w = _worker(_EMSettle({}))                   # el exchange aún no lo publica
    pos = {"symbol": "AAA", "exchange": "Binance", "ih": ih, "entry_time": (last_pay - 10000) * 1000,
           "last_earn_update": last_pay - 10000, "exposure": 1000, "capital_used": 2000,
           "earned_real": 0}
    cur = {"symbol": "AAA", "exchange": "Binance", "ih": ih, "fr": 0.0005,
           "next_funding_ts": int((last_pay + ih * 3600) * 1000)}
    w._update_spot_perp_earnings(pos, [cur], now)
    assert not pos.get("payments")              # se reintenta en el próximo tick


def test_manual_adjust_is_not_a_payment():
    w = _worker(_EMSettle({}))
    pos = {"symbol": "AAA", "earned_real": 5.0,
           "payments": [{"ts": 1, "rate": 0, "earned": 5.0, "kind": "manual_adjust"}]}
    w._record_earnings(pos, 1.0, 0.0002, 10.0, 1, payment_ts=9)
    assert pos["payment_count"] == 1 and pos["avg_rate"] == pytest.approx(0.0002)


def _alert_pos(last_fr, payment_count=3):
    return {"id": "7", "user_id": 1, "symbol": "AAA", "exchange": "Binance", "mode": "spot_perp",
            "entry_fr": 0.0003, "last_fr_used": last_fr, "payment_count": payment_count,
            "entry_price": 1.0, "leverage": 1}


def _cur(fr):
    return [{"symbol": "AAA", "exchange": "Binance", "fr": fr, "mins_next": 120,
             "next_funding_ts": 1, "price": 1.0}]


def test_close_suggested_after_two_non_positive_settlements():
    w = _worker(_EMSettle({}))
    alerts = w._check_alerts_db([_alert_pos(-0.0001)], _cur(-0.0002), {"AAA_Binance": {"pred": -12.0}})
    assert [a["type"] for a in alerts] == ["CLOSE_SUGGESTED"]
    assert "-12.0%" in alerts[0]["message"]


def test_single_negative_rate_is_only_informative_and_deduped_per_position():
    w = _worker(_EMSettle({}))
    alerts = w._check_alerts_db([_alert_pos(0.0002)], _cur(-0.0001))
    assert [a["type"] for a in alerts] == ["RATE_REVERSAL"]
    w._notified_alerts[alerts[0]["dedup_key"]] = time.time()
    cur = _cur(-0.0001)
    cur[0]["next_funding_ts"] = 2               # otra ventana de funding
    assert w._check_alerts_db([_alert_pos(0.0002)], cur) == []   # no re-dispara por ventana


def test_rate_drop_vs_entry_spike_no_longer_alerts():
    w = _worker(_EMSettle({}))
    assert w._check_alerts_db([_alert_pos(0.00005)], _cur(0.00005)) == []


# ── Switch ──────────────────────────────────────────────────────────────────
def test_switch_projection_keeps_sign_and_cost_is_new_round_trip(monkeypatch):
    from analysis import switch_analyzer as sa
    assert sa.calculate_projected_earnings(-0.0001, 3, 1000, 24) == pytest.approx(-0.3)
    monkeypatch.setattr(sa, "calculate_spot_perp_fees",
                        lambda ex, cap, vol, symbol=None: {"total_cost": 6.0, "exit_fees": 2.0,
                                                           "exit_slip": 0.5})
    cost = sa.calculate_switch_cost({"mode": "spot_perp", "exchange": "Binance"},
                                    {"mode": "spot_perp", "exchange": "Binance"}, 2000)
    assert cost["total_cost"] == pytest.approx(6.0)        # antes 12.0 (dos round-trips)


# ── Fees ────────────────────────────────────────────────────────────────────
def test_fee_loader_ignores_class_defaults_without_markets():
    from analysis import fee_loader

    class Inst:
        fees = {"trading": {"maker": 0.001, "taker": 0.001}}   # defaults spot de ccxt
        markets = None
    assert fee_loader._load_one("Binance", Inst()) == {}


def test_fee_loader_separates_spot_and_perp():
    from analysis import fee_loader

    class Inst:
        fees = {}
        markets = {
            "A/USDT": {"spot": True, "quote": "USDT", "maker": 0.001, "taker": 0.001},
            "A/USDT:USDT": {"swap": True, "contract": True, "quote": "USDT",
                            "maker": 0.0002, "taker": 0.0005},
        }
    out = fee_loader._load_one("Binance", Inst())
    assert out["spot_taker"] == pytest.approx(0.1) and out["fut_taker"] == pytest.approx(0.05)


# ── Caché de historial y scan horario ───────────────────────────────────────
def test_history_cache_until_next_settlement(monkeypatch):
    from exchanges import manager as m

    class Ex:
        markets = {"x": 1}
        n = 0

        def fetch_funding_rate_history(self, pair, limit=30):
            Ex.n += 1
            t0 = int(time.time() // 28800 * 28800 * 1000)
            return [{"fundingRate": 0.0001, "timestamp": t0 - 28800_000},
                    {"fundingRate": 0.0001, "timestamp": t0}]

    em = m.ExchangeManager.__new__(m.ExchangeManager)
    em._exchanges = {"binance": Ex()}
    em._history_cache = {}
    import threading
    em._history_lock = threading.Lock()
    em.fetch_funding_history("AAA", "Binance", 30)
    em.fetch_funding_history("AAA", "Binance", 30)
    assert Ex.n == 1                                   # segunda lectura desde caché
    em.fetch_funding_history("AAA", "Binance", 30, use_cache=False)
    assert Ex.n == 2


def test_periodic_scan_key_runs_hourly_after_settlement():
    w = _worker(_EMSettle({}))
    hour = 1_800_000_000 // 3600 * 3600
    assert w._periodic_scan_key(hour + 60) is None            # antes de :04
    assert w._periodic_scan_key(hour + 300) == f"periodic_{hour}"
    w._last_scan_ts = hour + 400
    assert w._periodic_scan_key(hour + 900) is None           # ya escaneó esta hora
