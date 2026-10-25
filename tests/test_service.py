import asyncio
import sqlite3
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from olga.events import Occurrence, PermanentError, TemporaryError
from olga.service import DailyRun, Ledger, run_service, send_daily

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 10, 24)


class StopRun(Exception):
    pass


class Clock:
    def __init__(self, hour=9, minute=0):
        self.value = datetime(2026, 10, 24, hour, minute, tzinfo=BERLIN)
        self.delays = []

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += timedelta(seconds=seconds)
        raise StopRun


def events(day=DAY):
    start = datetime.combine(day, datetime.min.time(), BERLIN) + timedelta(hours=12)
    return [Occurrence("1", "1", "Workshop", start, start + timedelta(hours=1))]


def run(clock, ledger, fetch, send):
    with pytest.raises(StopRun):
        asyncio.run(
            run_service(BERLIN, ledger, fetch, send, now=clock.now, sleep=clock.sleep)
        )


@pytest.fixture
def ledger(tmp_path):
    result = Ledger(tmp_path / "state.sqlite3", "calendar", -123)
    yield result
    result.close()


@pytest.mark.parametrize("hour", [9, 11, 23])
def test_at_nine_and_late_startup(ledger, hour):
    clock = Clock(hour)
    fetch = AsyncMock(return_value=events())
    send = AsyncMock(return_value=100)
    run(clock, ledger, fetch, send)
    fetch.assert_awaited_once_with(DAY)
    send.assert_awaited_once()
    assert ledger.completed(DAY)
    assert "Workshop" in send.call_args.args[0]


def test_before_nine_does_not_fetch_or_send(ledger):
    clock = Clock(8, 59)
    fetch, send = AsyncMock(), AsyncMock()
    run(clock, ledger, fetch, send)
    assert clock.delays == [60]
    fetch.assert_not_awaited()
    send.assert_not_awaited()


def test_empty_day_completed_without_sending(ledger):
    fetch, send = AsyncMock(return_value=[]), AsyncMock()
    run(Clock(), ledger, fetch, send)
    assert ledger.completed(DAY)
    run(Clock(11), ledger, fetch, send)
    fetch.assert_awaited_once()
    send.assert_not_awaited()


def test_ledger_persists_and_is_scoped(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = Ledger(path, "calendar", -123)
    first.complete(DAY, 100)
    first.close()
    second = Ledger(path, "calendar", -123)
    assert second.completed(DAY)
    second.close()
    different = Ledger(path, "calendar", -456)
    assert not different.completed(DAY)
    different.close()


def test_ledger_reads_original_sqlite_history(tmp_path):
    path = tmp_path / "original.sqlite3"
    # Reproduce the pre-SQLAlchemy schema and records without ORM-generated SQL.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE daily_runs ("
            "calendar TEXT NOT NULL, destination INTEGER NOT NULL, "
            "day TEXT NOT NULL, message_id INTEGER, "
            "PRIMARY KEY (calendar, destination, day))"
        )
        connection.executemany(
            "INSERT INTO daily_runs VALUES (?, ?, ?, ?)",
            [
                ("calendar", -1001234567890, DAY.isoformat(), 100),
                ("calendar", -1001234567890, "2026-10-23", None),
            ],
        )
    ledger = Ledger(path, "calendar", -1001234567890)
    try:
        assert ledger.completed(DAY)
        assert ledger.completed(date(2026, 10, 23))
        ledger.complete(date(2026, 10, 25), 101)
        with Session(ledger.engine) as session:
            record = session.get(DailyRun, ("calendar", -1001234567890, DAY))
            assert record.day == DAY
            assert record.message_id == 100
    finally:
        ledger.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT day, message_id FROM daily_runs ORDER BY day"
        ).fetchall() == [
            ("2026-10-23", None),
            ("2026-10-24", 100),
            ("2026-10-25", 101),
        ]


def test_duplicate_day_fails_without_overwriting_and_can_recover(ledger):
    ledger.complete(DAY, 100)
    with pytest.raises(IntegrityError):
        ledger.complete(DAY, 999)
    with Session(ledger.engine) as session:
        assert session.get(DailyRun, ("calendar", -123, DAY)).message_id == 100
    ledger.complete(date(2026, 10, 25), None)
    assert ledger.completed(date(2026, 10, 25))


def test_database_path_handles_url_characters(tmp_path):
    ledger = Ledger(tmp_path / "state?#.sqlite3", "calendar", -123)
    try:
        ledger.complete(DAY, None)
        assert ledger.completed(DAY)
        assert (tmp_path / "state?#.sqlite3").is_file()
    finally:
        ledger.close()


def test_restart_does_not_repeat(ledger):
    ledger.complete(DAY, 100)
    fetch, send = AsyncMock(), AsyncMock()
    run(Clock(15), ledger, fetch, send)
    fetch.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.parametrize("stage", ["fetch", "send"])
def test_transient_failure_retries_and_refreshes(ledger, stage):
    clock = Clock()
    fetch = AsyncMock(return_value=events())
    send = AsyncMock(return_value=100)
    if stage == "fetch":
        fetch.side_effect = [TemporaryError("offline"), events()]
    else:
        send.side_effect = [TemporaryError("offline"), 100]
    calls = 0

    async def sleep(seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise StopRun
        assert not ledger.completed(DAY)
        clock.value += timedelta(seconds=seconds)

    with pytest.raises(StopRun):
        asyncio.run(
            run_service(BERLIN, ledger, fetch, send, now=clock.now, sleep=sleep)
        )
    assert fetch.await_count == 2
    assert ledger.completed(DAY)


def test_telegram_rate_limit(ledger):
    clock = Clock()
    fetch = AsyncMock(return_value=events())
    send = AsyncMock(side_effect=RetryAfter(120))
    run(clock, ledger, fetch, send)
    assert clock.delays == [120]
    assert not ledger.completed(DAY)


def test_permanent_error_is_not_success(ledger):
    fetch = AsyncMock(return_value=events())
    send = AsyncMock(side_effect=PermanentError("permissions"))
    with pytest.raises(PermanentError):
        asyncio.run(run_service(BERLIN, ledger, fetch, send, now=Clock().now))
    assert not ledger.completed(DAY)


def test_midnight_cuts_off_retry(ledger):
    clock = Clock(23, 59)
    clock.value = clock.value.replace(second=50)
    fetch = AsyncMock(side_effect=TemporaryError("offline"))
    send = AsyncMock()
    run(clock, ledger, fetch, send)
    assert clock.delays == [10]
    assert not ledger.completed(DAY)


def test_query_finishing_after_midnight_does_not_send(ledger):
    clock = Clock(23, 59)

    async def fetch(day):
        clock.value += timedelta(minutes=2)
        return events(day)

    send = AsyncMock()
    run(clock, ledger, fetch, send)
    send.assert_not_awaited()
    assert not ledger.completed(DAY)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (NetworkError("secret URL"), TemporaryError),
        (Forbidden("secret URL"), PermanentError),
        (BadRequest("secret URL"), PermanentError),
        (RetryAfter(120), RetryAfter),
    ],
)
def test_send_errors(error, expected):
    bot = AsyncMock()
    bot.send_message.side_effect = error
    with pytest.raises(expected) as caught:
        asyncio.run(send_daily(bot, -123, "<literal>"))
    assert "secret URL" not in str(caught.value)


def test_sender_disables_markup():
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 7
    assert asyncio.run(send_daily(bot, -123, "<literal>")) == 7
    assert bot.send_message.call_args.kwargs["parse_mode"] is None


def test_multiple_events_send_one_message(ledger):
    first = events()[0]
    second = Occurrence("2", "2", "Second event", first.start, first.end)
    fetch = AsyncMock(return_value=[first, second])
    send = AsyncMock(return_value=100)
    run(Clock(), ledger, fetch, send)
    send.assert_awaited_once()
    text = send.call_args.args[0]
    assert "events are" in text
    assert "- Workshop:" in text
    assert "- Second event:" in text


def test_retry_refresh_honours_removed_event(ledger):
    clock = Clock()
    fetch = AsyncMock(side_effect=[events(), []])
    send = AsyncMock(side_effect=TemporaryError("offline"))
    attempts = 0

    async def sleep(seconds):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise StopRun
        clock.value += timedelta(seconds=seconds)

    with pytest.raises(StopRun):
        asyncio.run(
            run_service(BERLIN, ledger, fetch, send, now=clock.now, sleep=sleep)
        )
    assert fetch.await_count == 2
    send.assert_awaited_once()
    assert ledger.completed(DAY)
