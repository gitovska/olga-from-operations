import asyncio
import logging
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import SQLAlchemyError
from telegram.constants import ChatType

from olga.__main__ import configure_logging, discover, main
from olga.config import CalendarSettings, SecretFileError, chat_id, secret
from olga.events import PermanentError


def fail(*args, **kwargs):
    pytest.fail("Forbidden side effect")


def test_dry_run_isolated(monkeypatch, capsys):
    settings = CalendarSettings(
        "https://example.test/", ZoneInfo("Europe/Berlin"), "", ""
    )
    monkeypatch.setattr("sys.argv", ["olga", "dry-run"])
    monkeypatch.setattr("olga.__main__.CalendarSettings.load", lambda: settings)
    monkeypatch.setattr("olga.__main__.read_events", lambda settings, day: [])
    monkeypatch.setattr("olga.__main__.Bot", fail)
    monkeypatch.setattr("olga.__main__.Ledger", fail)
    monkeypatch.setattr("olga.__main__.secret", fail)
    monkeypatch.setattr("olga.__main__.chat_id", fail)
    main()
    assert capsys.readouterr().out == "No qualifying events today.\n"


@pytest.mark.parametrize(
    ("chat_type", "command", "destination"),
    [
        (ChatType.SUPERGROUP, "/chatid@OlgaBot", -12345),
        (ChatType.GROUP, "/chatid@OlgaBot", -12345),
        (ChatType.PRIVATE, "/chatid", 12345),
        (ChatType.PRIVATE, "/chatid@OlgaBot", 12345),
    ],
)
def test_discover_chat_isolated(monkeypatch, capsys, chat_type, command, destination):
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(username="OlgaBot")

    def update(number, text, kind=ChatType.SUPERGROUP):
        return SimpleNamespace(
            update_id=number,
            message=SimpleNamespace(
                chat=SimpleNamespace(type=kind), text=text, chat_id=destination
            ),
        )

    bot.get_updates.side_effect = [
        [
            update(1, "/chatid@OtherBot"),
            update(2, "/chatid", ChatType.GROUP),
            update(3, "   "),
        ],
        [update(4, command, chat_type)],
    ]
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=bot)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("olga.__main__.Bot", factory)
    monkeypatch.setattr("olga.__main__.Ledger", fail)
    monkeypatch.setattr("olga.__main__.read_events", fail)
    asyncio.run(discover("test-token", 1))
    assert f"TELEGRAM_CHAT_ID={destination}" in capsys.readouterr().out
    assert bot.get_updates.call_args.kwargs["offset"] == 4
    bot.send_message.assert_not_awaited()


def test_discovery_timeout(monkeypatch):
    bot = AsyncMock()

    async def slow(**kwargs):
        await asyncio.sleep(1)

    bot.get_updates.side_effect = slow
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=bot)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("olga.__main__.Bot", factory)
    with pytest.raises(PermanentError, match="timed out"):
        asyncio.run(discover("test-token", 0.01))
    bot.send_message.assert_not_awaited()


def test_calendar_configuration_and_secrets(tmp_path, monkeypatch):
    username = tmp_path / "username"
    password = tmp_path / "password"
    username.write_text("test-user\n")
    password.write_text("test-password\n")
    monkeypatch.setenv("CALDAV_USERNAME_FILE", str(username))
    monkeypatch.setenv("CALDAV_PASSWORD_FILE", str(password))
    monkeypatch.delenv("CALDAV_URL", raising=False)
    monkeypatch.delenv("TIMEZONE", raising=False)
    result = CalendarSettings.load()
    assert result.username == "test-user"
    assert result.password == "test-password"
    assert result.timezone.key == "Europe/Berlin"
    assert "test-password" not in repr(result)
    password.write_text("")
    with pytest.raises(ValueError, match="empty"):
        CalendarSettings.load()


@pytest.mark.parametrize(
    "name", ["caldav_username", "caldav_password", "telegram_bot_token"]
)
@pytest.mark.parametrize(
    "error", [FileNotFoundError, PermissionError, IsADirectoryError]
)
def test_unreadable_secret_has_safe_actionable_error(monkeypatch, name, error):
    setting = f"{name.upper()}_FILE"
    monkeypatch.setenv(setting, "/private/secret-path")

    def unreadable(path):
        raise error("private exception details")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(SecretFileError) as caught:
        secret(name)
    message = str(caught.value)
    assert name in message
    assert setting in message
    assert error.__name__ in message
    assert "/private/secret-path" not in message
    assert "private exception details" not in message


def test_missing_default_secret_exits_with_configuration_hint(monkeypatch, caplog):
    monkeypatch.setattr("sys.argv", ["olga", "dry-run"])
    monkeypatch.delenv("CALDAV_USERNAME_FILE", raising=False)
    monkeypatch.delenv("CALDAV_URL", raising=False)

    def missing(path):
        assert path == Path("/run/secrets/caldav_username")
        raise FileNotFoundError("private exception details")

    monkeypatch.setattr(Path, "read_text", missing)
    monkeypatch.setattr("olga.__main__.read_events", fail)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "FileNotFoundError" in caplog.text
    assert "CALDAV_USERNAME_FILE" in caplog.text
    assert "Docker secret mounts" in caplog.text
    assert "private exception details" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/",
        "https://user:password@example.test/",
        "https://example.test/?token=secret",
        "https://example.test/#secret",
    ],
)
def test_reject_unsafe_calendar_urls(monkeypatch, url):
    monkeypatch.setenv("CALDAV_URL", url)
    with pytest.raises(ValueError, match="HTTPS"):
        CalendarSettings.load()


@pytest.mark.parametrize("value", ["", "0", "not-an-id"])
def test_invalid_group_ids(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", value)
    with pytest.raises(ValueError):
        chat_id()


@pytest.mark.parametrize("value", [-10012345, 12345])
def test_chat_id(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(value))
    assert chat_id() == value


def test_dependency_logs_are_filtered():
    configure_logging()
    record = logging.LogRecord("httpx.client", logging.INFO, "", 0, "secret", (), None)
    handlers = logging.getLogger().handlers
    assert handlers
    assert all(not handler.filter(record) for handler in handlers)
    safe = logging.LogRecord("olga.service", logging.INFO, "", 0, "safe", (), None)
    assert all(handler.filter(safe) for handler in handlers)


def test_ledger_new_day_not_already_completed(tmp_path):
    from olga.service import Ledger

    ledger = Ledger(tmp_path / "state.sqlite3", "cal", -1)
    ledger.complete(date(2026, 10, 24), 1)
    assert not ledger.completed(date(2026, 10, 25))
    ledger.close()


def test_database_failure_exits_with_sanitized_error(monkeypatch, caplog):
    monkeypatch.setattr("sys.argv", ["olga", "run"])

    async def broken_run():
        raise SQLAlchemyError("private database details")

    monkeypatch.setattr("olga.__main__.run", broken_run)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert "SQLAlchemyError" in caplog.text
    assert "private database details" not in caplog.text
