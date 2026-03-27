"""Thread-safe state manager — v8.0 unified (no safe/aggr split)."""
import threading
import logging
from core.persistence import JSONPersistence

log = logging.getLogger("bot")


class StateManager:
    def __init__(self, persistence: JSONPersistence):
        self._lock = threading.RLock()
        self._persistence = persistence
        self._state = {
            # --- Configurable from frontend ---
            "total_capital": 1000,
            "scan_interval": 300,           # seconds
            "min_volume": 1_000_000,
            "min_apr": 10,
            "min_score": 40,
            "min_stability_days": 3,
            "max_positions": 5,
            "alert_minutes_before": 5,

            # --- WhatsApp Notifications (CallMeBot) ---
            "email_enabled": False,         # kept as "email_enabled" for compat
            "wa_phone": "",                 # Phone with country code (no +)
            "wa_apikey": "",                # CallMeBot API key

            # --- Operational state (not editable) ---
            "positions": [],
            "history": [],
            "total_earned": 0,
            "scan_count": 0,
            "all_data": [],
            "defi_data": [],
            "opportunities": [],        # Unified opportunity list
            "defi_opportunities": [],
            "coinglass_data": [],
            "scanning": False,
            "alerts": [],
            "status": "Iniciando...",
            "last_error": "",
            "last_scan_time": "—",
            "last_scan": 0,
        }

    def load(self) -> None:
        saved = self._persistence.load()
        with self._lock:
            for k, v in saved.items():
                if k in self._state:
                    self._state[k] = v
            # Migrate: ensure all positions have an id
            import uuid
            for pos in self._state["positions"]:
                if not pos.get("id"):
                    pos["id"] = str(uuid.uuid4())[:8]
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

    def set_scan_results(self, opportunities: list, all_data: list) -> None:
        with self._lock:
            self._state["opportunities"] = opportunities
            self._state["all_data"] = all_data

    def set_alerts(self, alerts: list) -> None:
        with self._lock:
            self._state["alerts"] = alerts
