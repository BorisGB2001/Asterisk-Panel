"""Settings from environment variables (/opt/asterisk-panel/.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("PANEL_DATA", "/opt/asterisk-panel/data")))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    session_secret: str = field(default_factory=lambda: os.environ.get("SESSION_SECRET", ""))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    session_hours: int = field(default_factory=lambda: _int("SESSION_HOURS", 12))

    ami_host: str = field(default_factory=lambda: os.environ.get("AMI_HOST", "127.0.0.1"))
    ami_port: int = field(default_factory=lambda: _int("AMI_PORT", 5038))
    ami_user: str = field(default_factory=lambda: os.environ.get("AMI_USER", "panel"))
    ami_secret: str = field(default_factory=lambda: os.environ.get("AMI_SECRET", ""))

    # the panel writes its Asterisk config files here (included from the main files in asterisk_etc)
    asterisk_etc: Path = field(default_factory=lambda: Path(os.environ.get("ASTERISK_ETC", "/etc/asterisk")))
    asterisk_dir: Path = field(default_factory=lambda: Path(os.environ.get("ASTERISK_PANEL_DIR", "/etc/asterisk/panel")))
    # PANEL_HOST / PANEL_PORT: the address of the panel, for links in notifications
    panel_host: str = field(default_factory=lambda: os.environ.get("PANEL_HOST", ""))
    panel_port: int = field(default_factory=lambda: _int("PANEL_PORT", 6443))
    panel_tls: str = field(default_factory=lambda: os.environ.get("PANEL_TLS", "internal"))
    ivr_map: Path = field(default_factory=lambda: Path(os.environ.get("IVR_MAP", "/etc/asterisk/panel/ivr_map.json")))
    recordings_dir: Path = field(default_factory=lambda: Path(os.environ.get("RECORDINGS_DIR", "/var/spool/asterisk/monitor")))
    # country assumed for national numbers without a country code (8 495 ..., 495 ...)
    default_region: str = field(default_factory=lambda: os.environ.get("DEFAULT_REGION", "RU"))
    # answered within this many seconds counts as "within SLA"
    sla_seconds: int = field(default_factory=lambda: _int("SLA_SECONDS", 20))
    tz: str = field(default_factory=lambda: os.environ.get("TZ", "Europe/Moscow"))
    collectors: bool = field(default_factory=lambda: _bool("PANEL_COLLECTORS", True))

    def problems(self) -> list[str]:
        out = []
        if not self.database_url:
            out.append("DATABASE_URL")
        if len(self.session_secret) < 32:
            out.append("SESSION_SECRET")
        return out


settings = Settings()
