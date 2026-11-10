"""Pausa entre tramos con guarda de liquidación y ventanas de espera (exec_study_20261110)."""
import time
from unittest.mock import MagicMock

import pytest

from exchanges import trade_executor as te
from execution import jobs as J


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(te.time, "sleep", lambda s: None)


def _ctx():
    return MagicMock(cancelled=lambda: False)


def _short(mark, entry=1.0, liq=2.0):
    cli = MagicMock()
    cli.fetch_positions.return_value = [{"contracts": 10, "side": "short", "markPrice": mark,
                                         "entryPrice": entry, "liquidationPrice": liq}]
    return cli


def test_near_liquidation_uses_path_fraction():
    assert not te.near_liquidation(_short(1.69), "X")
    assert te.near_liquidation(_short(1.70), "X")


def test_near_liquidation_read_error_is_not_hot():
    cli = MagicMock()
    cli.fetch_positions.side_effect = RuntimeError("timeout")
    assert te.near_liquidation(cli, "X") is False


def test_slice_pause_checks_once_a_minute_and_stops_when_hot():
    calls = []

    def hot():
        calls.append(1)
        return len(calls) == 3
    assert te.slice_pause(_ctx(), 1200, hot) is True
    assert len(calls) == 3


def test_slice_pause_waits_full_interval_when_calm():
    hot = MagicMock(return_value=False)
    assert te.slice_pause(_ctx(), 1200, hot) is False
    assert hot.call_count == 1200 // te.SLICE_HOT_CHECK_S


def test_exit_waits_longer_than_entry():
    buy, sell = J.wait_params("buy"), J.wait_params("sell")
    now = time.time()
    assert buy["deadline"] - now == pytest.approx(J.PATIENT_ENTRY_WINDOW_S, abs=5)
    assert sell["deadline"] - now == pytest.approx(J.PATIENT_EXIT_WINDOW_S, abs=5)
    assert buy["theta"] == J.PATIENT_ENTRY_BASIS and sell["theta"] == J.PATIENT_EXIT_BASIS
