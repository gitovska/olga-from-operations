import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import caldav
import recurring_ical_events
from caldav.lib.error import AuthorizationError, DAVError, NotFoundError
from icalendar import Calendar, Event
from niquests.exceptions import RequestException, SSLError

from olga.config import CalendarSettings

logger = logging.getLogger(__name__)


class PermanentError(Exception):
    """An operator must correct this error before trying again."""


class TemporaryError(Exception):
    """The daily run may be retried."""


@dataclass(frozen=True)
class Occurrence:
    uid: str
    instance: str
    title: str
    start: datetime
    end: datetime
    location: str = "Coworking"
    all_day: bool = False


def day_window(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, timezone),
        datetime.combine(day + timedelta(days=1), time.min, timezone),
    )


def local_datetime(value: object, timezone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("Expected a timed event")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def event_bounds(event: Event, timezone: ZoneInfo) -> tuple[datetime, datetime, bool]:
    raw_start = event.decoded("DTSTART")
    all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)
    if all_day:
        start = datetime.combine(raw_start, time.min, timezone)
        if "DTEND" in event:
            raw_end = event.decoded("DTEND")
            if not isinstance(raw_end, date) or isinstance(raw_end, datetime):
                raise ValueError("All-day finish must be a date")
            end = datetime.combine(raw_end, time.min, timezone)
        else:
            duration = (
                event.decoded("DURATION") if "DURATION" in event else timedelta(days=1)
            )
            if (
                not isinstance(duration, timedelta)
                or duration <= timedelta(0)
                or duration.seconds
                or duration.microseconds
            ):
                raise ValueError("All-day duration must be positive whole days")
            end = datetime.combine(raw_start + duration, time.min, timezone)
    else:
        start = local_datetime(raw_start, timezone)
        if "DTEND" in event:
            end = local_datetime(event.decoded("DTEND"), timezone)
        elif "DURATION" in event:
            duration = event.decoded("DURATION")
            if not isinstance(duration, timedelta):
                raise ValueError("Invalid event duration")
            end = (start.astimezone(UTC) + duration).astimezone(timezone)
        else:
            raise ValueError("Missing event finish")
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise ValueError("Event finish must follow its start")
    return start, end, all_day


def normalize_floating(calendar: Calendar, timezone: ZoneInfo) -> None:
    # Give floating recurrence dates the same timezone as floating DTSTART.
    for component in calendar.walk("VEVENT"):
        for key in ("DTSTART", "DTEND", "RECURRENCE-ID", "EXDATE", "RDATE"):
            values = component.get(key)
            if values is None:
                continue
            for prop in values if isinstance(values, list) else [values]:
                for item in getattr(prop, "dts", [prop]):
                    value = getattr(item, "dt", None)
                    if isinstance(value, datetime) and value.tzinfo is None:
                        item.dt = value.replace(tzinfo=timezone)


def occurrence(event: Event, day: date, timezone: ZoneInfo) -> Occurrence | None:
    if str(event.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start, end, all_day = event_bounds(event, timezone)
    if start.date() != day:
        return None
    title = str(event.get("SUMMARY", "")).strip()
    uid = str(event.get("UID", "")).strip()
    if not title or not uid:
        raise ValueError("Missing event title or UID")
    recurrence_id = (
        event.decoded("RECURRENCE-ID") if "RECURRENCE-ID" in event else start
    )
    if isinstance(recurrence_id, date) and not isinstance(recurrence_id, datetime):
        recurrence_id = datetime.combine(recurrence_id, time.min, timezone)
    identity = local_datetime(recurrence_id, timezone).astimezone(UTC).isoformat()
    location = " ".join(str(event.get("LOCATION", "")).split()) or "Coworking"
    return Occurrence(
        uid, identity, " ".join(title.split()), start, end, location, all_day
    )


def expand_calendar(
    calendar: Calendar, day: date, timezone: ZoneInfo
) -> list[Occurrence]:
    if not isinstance(calendar, Calendar):
        raise ValueError("Missing VCALENDAR data")
    normalize_floating(calendar, timezone)
    start, end = day_window(day, timezone)
    invalid = set()
    # The recurrence library repairs negative durations and supplies missing ends.
    # Reject those series first instead of announcing invented times.
    for event in calendar.walk("VEVENT"):
        try:
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            _, last, all_day = event_bounds(event, timezone)
            if all_day and "DTEND" not in event and "DURATION" not in event:
                # RFC 5545: a DATE start without an end lasts one calendar day.
                event.add("DTEND", last.date())
        except (ValueError, TypeError, KeyError) as exc:
            logger.error(
                "Skipping invalid event series: missing/invalid start or finish (%s)",
                type(exc).__name__,
            )
            invalid.add(str(event.get("UID", "")))
    calendar.subcomponents = [
        component
        for component in calendar.subcomponents
        if component.name != "VEVENT" or str(component.get("UID", "")) not in invalid
    ]
    events = recurring_ical_events.of(calendar).between(start, end)
    result = []
    for event in events:
        try:
            item = occurrence(event, day, timezone)
        except (ValueError, TypeError, KeyError) as exc:
            logger.error("Skipping invalid event (%s)", type(exc).__name__)
            continue
        if item is not None:
            result.append(item)
    return result


def read_events(settings: CalendarSettings, day: date) -> list[Occurrence]:
    start, end = day_window(day, settings.timezone)
    try:
        with caldav.DAVClient(
            url=settings.url,
            username=settings.username,
            password=settings.password,
            timeout=30,
            ssl_verify_cert=True,
        ) as client:
            calendar = caldav.Calendar(client=client, url=settings.url)
            resources = calendar.search(
                start=start.astimezone(UTC),
                end=end.astimezone(UTC),
                event=True,
                expand=False,
                post_filter=False,
            )
            found: dict[tuple[str, str], Occurrence] = {}
            for resource in resources:
                try:
                    items = expand_calendar(
                        resource.icalendar_instance, day, settings.timezone
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    logger.error(
                        "Skipping malformed calendar resource (%s)",
                        type(exc).__name__,
                    )
                    continue
                for item in items:
                    found[item.uid, item.instance] = item
    except (AuthorizationError, NotFoundError, SSLError) as exc:
        raise PermanentError(
            f"CalDAV access failed ({type(exc).__name__}); check URL, "
            "credentials, permissions, and TLS"
        ) from None
    except (DAVError, RequestException) as exc:
        raise TemporaryError(f"CalDAV request failed ({type(exc).__name__})") from None
    return sorted(
        found.values(),
        key=lambda item: (item.start.astimezone(UTC), item.uid, item.instance),
    )


def format_message(events: list[Occurrence]) -> str | None:
    if not events:
        return None
    single = len(events) == 1
    lines: list[str] = []
    if not single:
        lines.append("The following events are taking place today:")
    for event in events:
        finish = event.end.strftime(
            "%H:%M" if event.start.date() == event.end.date() else "%d %b %Y %H:%M"
        )
        preposition = "in" if event.location.casefold() == "coworking" else "at"
        place = f"{preposition} {event.location}"
        if event.all_day:
            last_day = event.end.date() - timedelta(days=1)
            duration = (
                "all day today"
                if last_day == event.start.date()
                else f"all day from today through {last_day:%d %b %Y}"
            )
            lines.append(
                f"{event.title} is taking place {place} {duration}."
                if single
                else f"- {event.title}: {duration} {place}"
            )
        elif single:
            lines.append(
                f"{event.title} is taking place {place} "
                f"from {event.start:%H:%M} to {finish} today."
            )
        else:
            lines.append(f"- {event.title}: {event.start:%H:%M} to {finish} {place}")
    lines.append("Thank you, Olga.")
    message = "\n".join(lines)
    # UTF-16 units conservatively cover Telegram's length accounting for emoji.
    if len(message.encode("utf-16-le")) // 2 > 4096:
        raise PermanentError(
            "Daily message exceeds Telegram's 4096-character limit; "
            "shorten calendar titles or reduce the event list"
        )
    return message
