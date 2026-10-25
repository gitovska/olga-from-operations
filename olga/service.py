import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import URL, Date, Integer, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from olga.events import (
    Occurrence,
    PermanentError,
    TemporaryError,
    day_window,
    format_message,
)

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class DailyRun(Base):
    __tablename__ = "daily_runs"

    calendar: Mapped[str] = mapped_column(Text, primary_key=True)
    destination: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(Integer)


class Ledger:
    def __init__(self, path: Path, calendar: str, destination: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            URL.create("sqlite", database=str(path.resolve())),
            hide_parameters=True,
        )
        self.calendar = calendar
        self.destination = destination
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)

    def completed(self, day: date) -> bool:
        with self.sessions() as session:
            return (
                session.get(DailyRun, (self.calendar, self.destination, day))
                is not None
            )

    def complete(self, day: date, message_id: int | None) -> None:
        with self.sessions.begin() as session:
            session.add(
                DailyRun(
                    calendar=self.calendar,
                    destination=self.destination,
                    day=day,
                    message_id=message_id,
                )
            )

    def close(self) -> None:
        self.engine.dispose()


async def send_daily(bot: Bot, destination: int, text: str) -> int:
    try:
        message = await bot.send_message(
            chat_id=destination,
            text=text,
            parse_mode=None,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=10,
            pool_timeout=10,
        )
    except RetryAfter:
        raise
    except BadRequest:
        raise PermanentError(
            "Telegram rejected the message; check group ID and posting permissions"
        ) from None
    except NetworkError as exc:
        raise TemporaryError(
            f"Telegram request failed ({type(exc).__name__})"
        ) from None
    except TelegramError as exc:
        raise PermanentError(
            f"Telegram delivery failed ({type(exc).__name__}); "
            "check token, group ID, membership, and posting permissions"
        ) from None
    return message.message_id


async def run_service(
    timezone: ZoneInfo,
    ledger: Ledger,
    fetch: Callable[[date], Awaitable[list[Occurrence]]],
    send: Callable[[str], Awaitable[int]],
    *,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    clock = now or (lambda: datetime.now(timezone))
    backoff = 30
    while True:
        current = clock().astimezone(timezone)
        day = current.date()
        due = datetime.combine(day, time(9), timezone)
        if current < due or ledger.completed(day):
            target = due if current < due else due + timedelta(days=1)
            # Recheck periodically in case the system clock changes.
            delay = (target.astimezone(UTC) - current.astimezone(UTC)).total_seconds()
            await sleep(min(60, max(0, delay)))
            backoff = 30
            continue
        try:
            events = await fetch(day)
            if clock().astimezone(timezone).date() != day:
                backoff = 30
                continue
            text = format_message(events)
            message_id = None if text is None else await send(text)
            ledger.complete(day, message_id)
            logger.info(
                "Completed daily run for %s (%d events, message ID %s)",
                day,
                len(events),
                message_id,
            )
            backoff = 30
        except (TemporaryError, RetryAfter) as exc:
            delay = backoff
            if isinstance(exc, RetryAfter):
                retry = exc.retry_after
                delay = (
                    retry.total_seconds()
                    if isinstance(retry, timedelta)
                    else float(retry)
                )
                delay = max(delay, backoff)
            logger.warning(
                "Daily run failed: %s; retrying in up to %s seconds",
                exc,
                delay,
            )
            _, midnight = day_window(day, timezone)
            remaining = (
                midnight.astimezone(UTC) - clock().astimezone(UTC)
            ).total_seconds()
            await sleep(max(0, min(delay, remaining)))
            backoff = min(backoff * 2, 900)
