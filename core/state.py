"""Thread-safe state manager — v10.0 SaaS (scan data only).

Per-user data (positions, history, config) is stored in PostgreSQL.
This state manager holds ONLY shared, volatile scan data.
"""
import threading
import logging
from core.persistence import JSONPersistence

log = logging.getLogger("bot")


class StateManager:
    def __init__(self, persistence: JSONPersistence):
        self._lock = threading.RLock()
        self._persistence = persistence
        self._state = {
            # --- Shared scan data (volatile, not per-user) ---
            "all_data": [],
            "defi_data": [],
            "opportunities": [],
            "defi_opportunities": [],
            "coinglass_data": [],
            "scan_count": 0,
            "scanning": False,
            "alerts": [],
            "status": "Iniciando...",
            "last_error": "",
            "last_scan_time": "—",
            "last_scan": 0,

            # --- Telegram config (synced from DB for notifier) ---
            "email_enabled": False,
            "tg_chat_id": "",
            "tg_bot_token": "",

            # --- Configuración editable (defaults, overridden by DB) ---
            "total_capital": 1000,
            "min_volume": 1_000_000,
            "min_apr": 10,
            "min_score": 40,
            "min_stability_days": 3,
            "max_positions": 5,
            "alert_minutes_before": 5,
        }

    def load(self) -> None:
        saved = self._persistence.load()
        with self._lock:
            for k, v in saved.items():
                if k in self._state:
                    self._state[k] = v
            log.info(f"State loaded: scan_count={self._state.get('scan_count', 0)}")

    def save(self) -> None:
        """Save only minimal state (scan count, config). No positions/history."""
        with self._lock:
            saveable = {k: v for k, v in self._state.items()
                        if k not in ("all_data", "defi_data", "opportunities",
                                     "defi_opportunities", "coinglass_data",
                                     "alerts", "scanning")}
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
    def state(self) -> dict:
        return self._state

    def set_scan_results(self, opportunities: list, all_data: list) -> None:
        with self._lock:
            self._state["opportunities"] = opportunities
            self._state["all_data"] = all_data

    def set_alerts(self, alerts: list) -> None:
        with self._lock:
            self._state["alerts"] = alerts
