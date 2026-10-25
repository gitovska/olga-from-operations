from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from caldav.lib.error import AuthorizationError, ReportError
from icalendar import Calendar

from olga.config import CalendarSettings
from olga.events import (
    Occurrence,
    PermanentError,
    TemporaryError,
    day_window,
    expand_calendar,
    format_message,
    read_events,
)

BERLIN = ZoneInfo("Europe/Berlin")
DAY = date(2026, 10, 24)


def calendar(*bodies: str) -> Calendar:
    text = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Olga Tests//EN\r\n"
    for body in bodies:
        text += "BEGIN:VEVENT\r\n" + body.replace("\n", "\r\n") + "\r\nEND:VEVENT\r\n"
    return Calendar.from_ical(text + "END:VCALENDAR\r\n")


def event(**changes: str | None) -> str:
    values = {
        "UID": "event-1",
        "SUMMARY": "Workshop",
        "DTSTART": "20261024T080000Z",
        "DTEND": "20261024T100000Z",
        "LOCATION": "Coworking",
        **changes,
    }
    return "\n".join(f"{key}:{value}" for key, value in values.items() if value)


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("90mil", "90mil"),
        ("90MIL main hall", "90MIL main hall"),
        ("studio90mil", "studio90mil"),
        ("Coworking", "Coworking"),
        ("coworking", "coworking"),
        (" COWORKING ", "COWORKING"),
        ("", "Coworking"),
        (None, "Coworking"),
        ("   ", "Coworking"),
        (r"Main hall\nUpstairs", "Main hall Upstairs"),
        ("Music & <tea> *room*", "Music & <tea> *room*"),
    ],
)
def test_locations(location, expected):
    events = expand_calendar(calendar(event(LOCATION=location)), DAY, BERLIN)
    assert len(events) == 1
    assert events[0].location == expected
    preposition = "in" if expected.casefold() == "coworking" else "at"
    for items in (events, events + events):
        assert f"{preposition} {expected}" in format_message(items)


def test_message_shape():
    events = expand_calendar(
        calendar(event(SUMMARY="Music & <tea> *party*")), DAY, BERLIN
    )
    assert format_message(events) == (
        "Music & <tea> *party* is taking place in Coworking "
        "from 10:00 to 12:00 today.\n"
        "Thank you, Olga."
    )
    text = format_message(events + events)
    assert "The following events are taking place" in text
    assert text.count("- Music") == 2
    assert "A reminder from Olga from Operations:" not in text
    assert format_message([]) is None


def test_combined_message_with_different_locations():
    events = expand_calendar(
        calendar(
            event(UID="workshop", SUMMARY="Workshop", LOCATION=None),
            event(UID="music", SUMMARY="Music", LOCATION="90mil"),
        ),
        DAY,
        BERLIN,
    )
    assert format_message(events) == (
        "The following events are taking place today:\n"
        "- Workshop: 10:00 to 12:00 in Coworking\n"
        "- Music: 10:00 to 12:00 at 90mil\n"
        "Thank you, Olga."
    )


def test_multiline_title_becomes_one_list_entry():
    events = expand_calendar(
        calendar(event(SUMMARY=r"Workshop\nPart two")), DAY, BERLIN
    )
    assert events[0].title == "Workshop Part two"


def test_day_filter_and_early_event():
    data = calendar(
        event(UID="early", DTSTART="20261023T220000Z", DTEND="20261023T230000Z"),
        event(UID="old", DTSTART="20261023T210000Z", DTEND="20261024T050000Z"),
        event(UID="tomorrow", DTSTART="20261024T220000Z", DTEND="20261024T230000Z"),
    )
    assert [e.uid for e in expand_calendar(data, DAY, BERLIN)] == ["early"]


def test_all_day_without_finish_lasts_one_day():
    body = "UID:all-day\nSUMMARY:Day\nDTSTART;VALUE=DATE:20261024"
    events = expand_calendar(calendar(body), DAY, BERLIN)
    assert len(events) == 1
    assert events[0].all_day
    assert events[0].end.date() == date(2026, 10, 25)
    assert format_message(events) == (
        "Day is taking place in Coworking all day today.\nThank you, Olga."
    )


@pytest.mark.parametrize("finish", ["DTEND;VALUE=DATE:20261027", "DURATION:P3D"])
def test_multi_day_all_day_only_announced_on_start(finish):
    body = (
        "UID:festival\nSUMMARY:Festival\nLOCATION:90mil\n"
        f"DTSTART;VALUE=DATE:20261024\n{finish}"
    )
    events = expand_calendar(calendar(body), DAY, BERLIN)
    assert len(events) == 1
    assert format_message(events) == (
        "Festival is taking place at 90mil "
        "all day from today through 26 Oct 2026.\n"
        "Thank you, Olga."
    )
    for day in (date(2026, 10, 25), date(2026, 10, 26), date(2026, 10, 27)):
        assert expand_calendar(calendar(body), day, BERLIN) == []


def test_mixed_all_day_and_timed_message():
    body = (
        "UID:all-day\nSUMMARY:Open day\nDTSTART;VALUE=DATE:20261024\n"
        "DTEND;VALUE=DATE:20261025"
    )
    events = expand_calendar(calendar(body, event()), DAY, BERLIN)
    assert format_message(events) == (
        "The following events are taking place today:\n"
        "- Open day: all day today in Coworking\n"
        "- Workshop: 10:00 to 12:00 in Coworking\n"
        "Thank you, Olga."
    )


def test_recurring_multi_day_all_day_exceptions():
    master = (
        "UID:festival\nSUMMARY:Festival\nDTSTART;VALUE=DATE:20261024\n"
        "DTEND;VALUE=DATE:20261027\nRRULE:FREQ=WEEKLY;COUNT=3\n"
        "EXDATE;VALUE=DATE:20261031"
    )
    cancelled = (
        "UID:festival\nSUMMARY:Festival\n"
        "RECURRENCE-ID;VALUE=DATE:20261107\n"
        "DTSTART;VALUE=DATE:20261107\nDTEND;VALUE=DATE:20261110\nSTATUS:CANCELLED"
    )
    assert len(expand_calendar(calendar(master, cancelled), DAY, BERLIN)) == 1
    for day in (date(2026, 10, 25), date(2026, 10, 31), date(2026, 11, 7)):
        assert expand_calendar(calendar(master, cancelled), day, BERLIN) == []


def test_all_day_moved_recurrence_uses_actual_start():
    master = (
        "UID:day\nSUMMARY:Day\nDTSTART;VALUE=DATE:20261023\n"
        "DTEND;VALUE=DATE:20261024\nRRULE:FREQ=WEEKLY;COUNT=2"
    )
    moved = (
        "UID:day\nSUMMARY:Moved day\nRECURRENCE-ID;VALUE=DATE:20261023\n"
        "DTSTART;VALUE=DATE:20261024\nDTEND;VALUE=DATE:20261026"
    )
    assert expand_calendar(calendar(master, moved), date(2026, 10, 23), BERLIN) == []
    events = expand_calendar(calendar(master, moved), DAY, BERLIN)
    assert len(events) == 1
    assert events[0].title == "Moved day"
    assert expand_calendar(calendar(master, moved), date(2026, 10, 25), BERLIN) == []


@pytest.mark.parametrize(
    "finish",
    ["DTEND;VALUE=DATE:20261023", "DTEND:20261025T000000Z", "DURATION:PT2H"],
)
def test_invalid_all_day_finishes_are_logged(finish, caplog):
    body = f"UID:day\nSUMMARY:Day\nDTSTART;VALUE=DATE:20261024\n{finish}"
    assert expand_calendar(calendar(body), DAY, BERLIN) == []
    assert "Skipping invalid event series" in caplog.text


def test_all_day_across_dst_keeps_calendar_dates():
    body = "UID:day\nSUMMARY:Day\nDTSTART;VALUE=DATE:20261024\nDURATION:P3D"
    events = expand_calendar(calendar(body), DAY, BERLIN)
    assert events[0].end.date() == date(2026, 10, 27)
    assert "through 26 Oct 2026" in format_message(events)


def test_floating_times_and_duration():
    body = event(
        DTSTART="20261024T080000",
        DTEND=None,
        DURATION="PT2H",
    )
    item = expand_calendar(calendar(body), DAY, BERLIN)[0]
    assert item.start.hour == 8
    assert item.end.hour == 10
    assert item.start.tzinfo == BERLIN


def test_explicit_timezone_and_overnight_finish():
    body = (
        "UID:night\nSUMMARY:Night shift\n"
        "DTSTART;TZID=Europe/Berlin:20261024T230000\n"
        "DTEND;TZID=Europe/Berlin:20261025T020000"
    )
    text = format_message(expand_calendar(calendar(body), DAY, BERLIN))
    assert (
        "Night shift is taking place in Coworking "
        "from 23:00 to 25 Oct 2026 02:00 today."
    ) in text


@pytest.mark.parametrize(
    ("day", "hours"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)]
)
def test_dst_day_boundaries(day, hours):
    start, end = day_window(day, BERLIN)
    assert (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() == hours * 3600


def test_recurrence_exclusion_moved_and_cancelled_instances():
    master = event(
        DTSTART="20261022T080000Z",
        DTEND="20261022T090000Z",
        RRULE="FREQ=DAILY;COUNT=6",
        EXDATE="20261023T080000Z",
    )
    moved = event(
        **{
            "RECURRENCE-ID": "20261024T080000Z",
            "DTSTART": "20261025T120000Z",
            "DTEND": "20261025T130000Z",
            "SUMMARY": "Moved workshop",
            "LOCATION": "90mil",
        }
    )
    cancelled = event(
        **{
            "RECURRENCE-ID": "20261026T080000Z",
            "DTSTART": "20261026T080000Z",
            "DTEND": "20261026T090000Z",
            "STATUS": "CANCELLED",
        }
    )
    for day in (date(2026, 10, 23), DAY, date(2026, 10, 26)):
        assert expand_calendar(calendar(master, moved, cancelled), day, BERLIN) == []
    events = expand_calendar(
        calendar(master, moved, cancelled), date(2026, 10, 25), BERLIN
    )
    assert len(events) == 2
    assert len({e.instance for e in events}) == 2
    assert {e.title for e in events} == {"Workshop", "Moved workshop"}
    assert next(e for e in events if e.title == "Moved workshop").location == "90mil"


def test_floating_recurrence_exdate():
    body = event(
        DTSTART="20261023T080000",
        DTEND="20261023T090000",
        RRULE="FREQ=DAILY;COUNT=3",
        EXDATE="20261024T080000",
    )
    assert expand_calendar(calendar(body), DAY, BERLIN) == []


@pytest.mark.parametrize("missing", ["SUMMARY", "UID"])
def test_invalid_occurrence_logged(missing, caplog):
    assert expand_calendar(calendar(event(**{missing: None})), DAY, BERLIN) == []
    assert "Skipping invalid event" in caplog.text


def test_cancelled_event():
    assert expand_calendar(calendar(event(STATUS="CANCELLED")), DAY, BERLIN) == []


def test_finish_must_follow_start(caplog):
    assert expand_calendar(calendar(event(DTEND="20261024T070000Z")), DAY, BERLIN) == []
    assert "Skipping invalid event" in caplog.text


def test_exact_message_limit():
    start = datetime(2026, 10, 24, 10, tzinfo=BERLIN)
    item = Occurrence("1", "1", "", start, start + timedelta(hours=1))
    base = format_message([item])
    allowed = 4096 - len(base)
    full = Occurrence("1", "1", "x" * allowed, start, item.end)
    assert len(format_message([full])) == 4096
    with pytest.raises(PermanentError, match="4096"):
        format_message([Occurrence("1", "1", full.title + "x", start, item.end)])
    with pytest.raises(PermanentError):
        format_message([Occurrence("1", "1", "\U0001f600" * allowed, start, item.end)])


def mock_calendar(monkeypatch, resources):
    client = MagicMock()
    monkeypatch.setattr("olga.events.caldav.DAVClient", client)
    target = MagicMock()
    target.search.return_value = [
        SimpleNamespace(icalendar_instance=data) for data in resources
    ]
    factory = MagicMock(return_value=target)
    monkeypatch.setattr("olga.events.caldav.Calendar", factory)
    return client, factory, target


def settings():
    return CalendarSettings(
        "https://example.test/calendar/", BERLIN, "user", "password"
    )


def test_reader_query_sort_deduplicate_and_bad_resource(monkeypatch, caplog):
    data = calendar(
        event(UID="later", DTSTART="20261024T120000Z", DTEND="20261024T130000Z")
    )
    client, factory, target = mock_calendar(
        monkeypatch,
        [data, calendar(event(UID="earlier")), data, calendar(event(DTEND=None))],
    )
    result = read_events(settings(), DAY)
    assert [e.uid for e in result] == ["earlier", "later"]
    assert "Skipping invalid event series" in caplog.text
    assert factory.call_args.kwargs["url"] == settings().url
    assert client.call_args.kwargs["ssl_verify_cert"] is True
    assert client.call_args.kwargs["timeout"] == 30
    assert target.search.call_args.kwargs == {
        "start": datetime(2026, 10, 23, 22, tzinfo=UTC),
        "end": datetime(2026, 10, 24, 22, tzinfo=UTC),
        "event": True,
        "expand": False,
        "post_filter": False,
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthorizationError(reason="secret"), PermanentError),
        (ReportError(), TemporaryError),
    ],
)
def test_calendar_failures_are_explicit_and_sanitized(monkeypatch, error, expected):
    _, _, target = mock_calendar(monkeypatch, [])
    target.search.side_effect = error
    with pytest.raises(expected) as caught:
        read_events(settings(), DAY)
    assert "secret" not in str(caught.value)
