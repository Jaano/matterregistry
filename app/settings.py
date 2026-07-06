"""
Runtime settings: HA App mode (SUPERVISOR_TOKEN present) or standalone Docker.
"""

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_VALID_LOG_LEVELS = {"trace", "debug", "info", "warning", "error", "critical"}


class Settings:
    def __init__(self) -> None:
        self.supervisor_token: str | None = os.environ.get("SUPERVISOR_TOKEN")
        self.ha_mode: bool = bool(self.supervisor_token)
        self.data_dir: Path = Path("/config")
        self.db_path: Path = self.data_dir / "matterregistry.db"
        self.database_url: str = f"sqlite:///{self.db_path}"
        self.version: str = self._read_version()
        self._options: dict = self._read_options()
        self.log_level: str = self._read_log_level()
        self.option_python_matter_server_url: str = str(
            self._options.get("python_matter_server_url", "")
        ).strip()
        self.option_otbr_url: str = str(self._options.get("otbr_url", "")).strip()
        self.option_ha_core_url: str = str(self._options.get("ha_core_url", "")).strip()
        self.option_ha_core_token: str = str(self._options.get("ha_core_token", "")).strip()
        self.integration_sync_interval: int = self._read_sync_interval()
        self.mdns_enabled: bool = self._read_mdns_enabled()
        self.option_direct_api: bool = bool(self._options.get("direct_api", False))

    def _read_mdns_enabled(self) -> bool:
        """mDNS HomeKit discovery is opt-in (needs host networking)."""
        if self.ha_mode:
            return bool(self._options.get("mdns_enabled", False))
        return os.environ.get("MDNS_ENABLED", "").lower() in ("1", "true", "yes", "on")

    def _read_version(self) -> str:
        try:
            return version("matterregistry")
        except PackageNotFoundError:
            return "dev"

    def _read_options(self) -> dict:
        if not self.ha_mode:
            return {}
        try:
            return json.loads(Path("/data/options.json").read_text())
        except Exception:
            return {}

    def _read_sync_interval(self) -> int:
        if self.ha_mode:
            raw = str(self._options.get("integration_sync_interval", 600))
        else:
            raw = os.environ.get("MR_INTEGRATION_SYNC_INTERVAL", "600")
        try:
            val = int(raw)
        except ValueError:
            raise ValueError(f"MR_INTEGRATION_SYNC_INTERVAL: expected int, got {raw!r}")
        if val < -1:
            raise ValueError(
                f"MR_INTEGRATION_SYNC_INTERVAL must be -1 (disabled), 0 (startup only), "
                f"or a positive number of seconds; got {val}"
            )
        return val

    def _read_log_level(self) -> str:
        if self.ha_mode:
            level = str(self._options.get("log_level", "info")).lower()
            if level in _VALID_LOG_LEVELS:
                return level
        level = os.environ.get("MR_LOG_LEVEL", "info").lower()
        return level if level in _VALID_LOG_LEVELS else "info"


settings = Settings()
