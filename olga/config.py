import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

DEFAULT_CALENDAR = (
    "https://cal.rhonda.diy/coworker/e0678d6a-5b54-f221-31d0-7faca3d3c5b0/"
)


class SecretFileError(ValueError):
    pass


def secret(name: str) -> str:
    setting = f"{name.upper()}_FILE"
    path = Path(os.environ.get(setting, f"/run/secrets/{name}"))
    try:
        value = path.read_text().strip()
    except OSError as exc:
        raise SecretFileError(
            f"Cannot read secret file for {name} ({type(exc).__name__}); "
            f"set {setting} to a readable secret file. "
            "Default /run/secrets paths require Docker secret mounts."
        ) from None
    if not value:
        raise ValueError(f"Secret file for {name} is empty")
    return value


@dataclass(frozen=True)
class CalendarSettings:
    url: str
    timezone: ZoneInfo
    username: str = field(repr=False)
    password: str = field(repr=False)

    @classmethod
    def load(cls) -> Self:
        url = os.environ.get("CALDAV_URL", DEFAULT_CALENDAR)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CALDAV_URL must be HTTPS without credentials or query")
        return cls(
            url,
            ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin")),
            secret("caldav_username"),
            secret("caldav_password"),
        )


def chat_id() -> int:
    value = int(os.environ["TELEGRAM_CHAT_ID"])
    if value == 0:
        raise ValueError("TELEGRAM_CHAT_ID must be a nonzero numeric chat ID")
    return value


def state_path() -> Path:
    return Path(os.environ.get("STATE_PATH", "/data/olga.sqlite3"))
