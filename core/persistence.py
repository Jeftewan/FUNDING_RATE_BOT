"""JSON file persistence backend."""
import json
import os
import logging

log = logging.getLogger("bot")


class JSONPersistence:
    def __init__(self, file_path: str):
        self.file_path = file_path
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                self.file_path = os.path.join(".", os.path.basename(file_path))

    def load(self) -> dict:
        try:
            with open(self.file_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            log.info("No prior state file found")
            return {}
        except Exception as e:
            log.error(f"Load error: {e}")
            return {}

    def save(self, state: dict) -> None:
        try:
            tmp = self.file_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, self.file_path)
        except Exception as e:
            log.error(f"Save error: {e}")
