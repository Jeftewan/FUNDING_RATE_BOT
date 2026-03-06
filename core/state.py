"""Thread-safe state manager."""
import threading
import logging
from core.models import Position
from core.persistence import JSONPersistence

log = logging.getLogger("bot")


class StateManager:
    def __init__(self, persistence: JSONPersistence):
        self._lock = threading.RLock()
        self._persistence = persistence
        self._state = {
            "total_capital": 1000,
            "scan_interval": 300,
            "min_volume": 1000000,
            "safe_pct": 80, "aggr_pct": 20,
            "reserve_pct": 10,
            "max_pos_safe": 1, "max_pos_aggr": 1,
            "min_apr_safe": 5, "min_apr_aggr": 15, "min_score": 40,
            "positions": [], "history": [],
            "total_earned": 0, "scan_count": 0,
            "safe_top": [], "aggr_top": [],
            "all_data": [],
            "actions": [], "last_scan_time": "—",
            "status": "Iniciando...", "last_error": "",
            "skipped_tokens": [],
            # v7 additions
            "spot_perp_opportunities": [],
            "cross_exchange_opportunities": [],
            "alerts": [],
            "last_scan": 0,
        }

    def load(self) -> None:
        saved = self._persistence.load()
        with self._lock:
            for k, v in saved.items():
                if k in self._state:
                    self._state[k] = v
            # Convert position dicts to Position objects then back
            # (keep as dicts for backward compat)
            pos_count = len(self._state["positions"])
            earned = self._state.get("total_earned", 0)
            log.info(f"State loaded: {pos_count} pos, ${earned:.2f} earned")

    def save(self) -> None:
        with self._lock:
            saveable = {k: v for k, v in self._state.items()
                        if k not in ("all_data",)}
            self._persistence.save(saveable)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def get(self, key: str, default=None):
        with self._lock:
            return self._state.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._state[key] = value

    def update(self, **kwargs) -> None:
        with self._lock:
            self._state.update(kwargs)

    @property
    def positions(self) -> list:
        return self._state["positions"]

    @property
    def state(self) -> dict:
        return self._state

    def add_position(self, pos_dict: dict) -> None:
        with self._lock:
            self._state["positions"].append(pos_dict)
            self.save()

    def remove_position(self, idx: int) -> dict:
        with self._lock:
            if 0 <= idx < len(self._state["positions"]):
                return self._state["positions"].pop(idx)
            return {}

    def set_scan_results(self, spot_perp: list, cross_exchange: list,
                         all_data: list, safe_top: list, aggr_top: list) -> None:
        with self._lock:
            self._state["spot_perp_opportunities"] = spot_perp
            self._state["cross_exchange_opportunities"] = cross_exchange
            self._state["all_data"] = all_data
            self._state["safe_top"] = safe_top
            self._state["aggr_top"] = aggr_top

    def set_alerts(self, alerts: list) -> None:
        with self._lock:
            self._state["alerts"] = alerts
