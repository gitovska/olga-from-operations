import argparse
import asyncio
import logging
import signal
import sys
from datetime import date, datetime

from sqlalchemy.exc import SQLAlchemyError
from telegram import Bot
from telegram.constants import ChatType
from telegram.error import TelegramError

from olga.config import CalendarSettings, SecretFileError, chat_id, secret, state_path
from olga.events import (
    Occurrence,
    PermanentError,
    TemporaryError,
    format_message,
    read_events,
)
from olga.service import Ledger, run_service, send_daily

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # HTTP request URLs contain Telegram tokens; CalDAV diagnostics may contain
    # authenticated requests. Never enable these dependency logs in production.
    blocked = ("httpx", "httpcore", "caldav", "niquests", "urllib3", "urllib3_future")
    for handler in logging.getLogger().handlers:
        handler.addFilter(
            lambda record: (
                not any(
                    record.name == name or record.name.startswith(f"{name}.")
                    for name in blocked
                )
            )
        )


async def discover(token: str, timeout: float) -> None:
    async with Bot(token) as bot:
        me = await bot.get_me()
        print(
            f"Send /chatid privately to @{me.username}, "
            f"or /chatid@{me.username} in a group, "
            f"within {timeout:g} seconds.",
            flush=True,
        )
        offset = None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    updates = await bot.get_updates(
                        offset=offset,
                        timeout=10,
                        read_timeout=15,
                        allowed_updates=["message"],
                    )
                    for update in updates:
                        offset = update.update_id + 1
                        message = update.message
                        if (
                            message is not None
                            and message.chat.type
                            in (ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP)
                            and message.text is not None
                            and message.text.strip()
                            and (
                                message.text.split()[0].casefold()
                                == f"/chatid@{me.username}".casefold()
                                or message.chat.type == ChatType.PRIVATE
                                and message.text.split()[0].casefold() == "/chatid"
                            )
                        ):
                            print(f"TELEGRAM_CHAT_ID={message.chat_id}", flush=True)
                            return
        except TimeoutError:
            raise PermanentError(
                "Discovery timed out; send /chatid privately or the "
                "addressed command in the group "
                "and run discovery again"
            ) from None


async def run() -> None:
    task = asyncio.current_task()
    if task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    settings = CalendarSettings.load()
    destination = chat_id()
    token = secret("telegram_bot_token")
    ledger = Ledger(state_path(), settings.url, destination)
    try:
        async with Bot(token) as bot:

            async def fetch(day: date) -> list[Occurrence]:
                return await asyncio.to_thread(read_events, settings, day)

            async def send(text: str) -> int:
                return await send_daily(bot, destination, text)

            await run_service(settings.timezone, ledger, fetch, send)
    finally:
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Olga from Operations")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("run", help="Run the daily reminder service")
    subcommands.add_parser("dry-run", help="Print today's message without sending")
    discovery = subcommands.add_parser(
        "discover-chat", help="Find a private or group chat ID"
    )
    discovery.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.command == "discover-chat" and not 0 < args.timeout <= 600:
        parser.error("--timeout must be greater than 0 and at most 600 seconds")
    configure_logging()
    try:
        if args.command == "dry-run":
            settings = CalendarSettings.load()
            events = read_events(settings, datetime.now(settings.timezone).date())
            text = format_message(events)
            print(text if text is not None else "No qualifying events today.")
        elif args.command == "discover-chat":
            asyncio.run(discover(secret("telegram_bot_token"), args.timeout))
        else:
            asyncio.run(run())
    except (PermanentError, TemporaryError, SecretFileError) as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except (OSError, ValueError, KeyError, SQLAlchemyError, TelegramError) as exc:
        # Avoid printing dependency exception text: it can contain request URLs.
        logger.error(
            "Olga stopped (%s); check configuration, secret files, state "
            "permissions, and network access",
            type(exc).__name__,
        )
        sys.exit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Olga stopped")


if __name__ == "__main__":
    main()
