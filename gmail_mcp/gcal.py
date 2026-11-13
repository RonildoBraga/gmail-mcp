"""Google Calendar helpers. Calendar is Google-only, like Drive.

Events come back in a compact shape. All-day events use dates, and their `end`
is the event's last day. Google stores the day after instead, so this module
converts in both directions and callers never see the exclusive form.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googleapiclient.discovery import build

from . import auth, mailtext

_SCOPE = "https://www.googleapis.com/auth/"
READ_EVENTS = frozenset(_SCOPE + s for s in (
    "calendar", "calendar.readonly", "calendar.events", "calendar.events.readonly"))
WRITE_EVENTS = frozenset(_SCOPE + s for s in ("calendar", "calendar.events"))
READ_CALENDARS = frozenset(_SCOPE + s for s in (
    "calendar", "calendar.readonly", "calendar.calendarlist", "calendar.calendarlist.readonly"))

SEND_UPDATES = ("none", "all", "externalOnly")
EVENT_FIELDS = ("id,status,htmlLink,summary,description,location,start,end,recurringEventId,"
                "eventType,organizer(email),attendees(email,responseStatus)")
LIST_DESCRIPTION_CHARS = 500
EVENT_DESCRIPTION_CHARS = 20_000


def service_for(account: str | None, *needs: frozenset[str]):
    """Return (email, Calendar v3 service) once the account has granted every need.

    Each need is a set of scopes, any one of which allows the operation. A missing
    grant fails before any token refresh, with reconnect guidance.
    """
    email, provider, auth_data = auth.resolve_provider(account)
    if provider != "google":
        raise ValueError(f"Calendar is only available for Google accounts ({email!r} is {provider}).")
    granted = set(auth_data.get("scopes") or [])
    if any(not need & granted for need in needs or (READ_EVENTS,)):
        raise PermissionError(
            f"{email!r} hasn't given this server the calendar access this needs. Run add_account, "
            f"sign in as {email} and allow Calendar. Mail and Drive keep working meanwhile."
        )
    creds = auth.google_credentials(email, auth_data)
    return email, build("calendar", "v3", credentials=creds, cache_discovery=False)


def check_send_updates(value: str) -> str:
    if value not in SEND_UPDATES:
        raise ValueError(f"send_updates must be one of {', '.join(SEND_UPDATES)}.")
    return value


def zone_info(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone {name!r}; use a name like 'Europe/London'.") from None


def parse_time(value: str, name: str) -> date | datetime:
    """Parse a date (2027-03-01) or a time (2027-03-01T15:00, offset optional)."""
    text = value.strip() if isinstance(value, str) else ""
    try:
        if text and not any(mark in text for mark in "T :"):
            return date.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{name} must be a date like 2027-03-01 or a time like 2027-03-01T15:00:00+09:00, "
            f"got {value!r}."
        ) from None


def _is_time(value: date | datetime) -> bool:
    return isinstance(value, datetime)


def bound_needs_zone(value: str | None) -> bool:
    """Whether a list bound needs a zone: dates and times without an offset do."""
    if value is None:
        return False
    parsed = parse_time(value, "time bound")
    return not _is_time(parsed) or parsed.tzinfo is None


def times_need_zone(start: str | None, end: str | None) -> bool:
    """Whether event times need a zone: only times without an offset do."""
    parsed = [parse_time(v, n) for v, n in ((start, "start"), (end, "end")) if v is not None]
    return any(_is_time(p) and p.tzinfo is None for p in parsed)


def bound(value: str, zone: str | None, *, end: bool = False) -> str:
    """RFC 3339 bound for events.list. A date covers that whole day."""
    parsed = parse_time(value, "time_max" if end else "time_min")
    if _is_time(parsed):
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_required_zone(zone))
        return parsed.isoformat()
    day = parsed + timedelta(days=1) if end else parsed
    return datetime.combine(day, time.min, _required_zone(zone)).isoformat()


def _required_zone(zone: str | None) -> ZoneInfo:
    if not zone:
        raise ValueError("Times without a UTC offset need a time zone.")
    return zone_info(zone)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event_times(start: str, end: str, zone: str | None, *, clear_other: bool = False) -> tuple[dict, dict]:
    """Build Google's start/end objects; an all-day `end` is the last day, inclusive.

    clear_other nulls the other representation, so a patch can turn an all-day
    event into a timed one or back.
    """
    first, last = parse_time(start, "start"), parse_time(end, "end")
    if _is_time(first) != _is_time(last):
        raise ValueError("start and end must both be dates (an all-day event) or both be times.")
    if not _is_time(first):
        if last < first:
            raise ValueError("end is the event's last day, so it can't be before start.")
        extra = {"dateTime": None, "timeZone": None} if clear_other else {}
        return ({"date": first.isoformat(), **extra},
                {"date": (last + timedelta(days=1)).isoformat(), **extra})
    if (first.tzinfo is None) != (last.tzinfo is None):
        raise ValueError("Give both times with a UTC offset, or neither.")
    if first.tzinfo is None:
        _required_zone(zone)
    elif zone:
        zone_info(zone)
    if last <= first:
        raise ValueError("end must be after start.")

    def render(moment: datetime) -> dict:
        out = {"dateTime": moment.isoformat()}
        if zone:
            out["timeZone"] = zone
        if clear_other:
            out["date"] = None
        return out

    return render(first), render(last)


def check_times(start: str, end: str, zone: str | None) -> None:
    """Validate event times before credentials are used; the zone may be looked up later."""
    event_times(start, end, zone or "UTC")


def attendee_list(emails: list[str]) -> list[dict]:
    cleaned = [e.strip() for e in emails]
    bad = [e for e in cleaned if "@" not in e or any(c.isspace() for c in e)]
    if bad:
        raise ValueError(f"Attendees must be email addresses: {bad}")
    return [{"email": e} for e in cleaned]


def _last_day(exclusive_end: str | None) -> str | None:
    if not exclusive_end:
        return exclusive_end
    return (date.fromisoformat(exclusive_end) - timedelta(days=1)).isoformat()


def simplify(event: dict, *, max_description: int = EVENT_DESCRIPTION_CHARS, link: bool = True) -> dict:
    """Compact view of an event, with an all-day `end` given as its last day."""
    start, end = event.get("start") or {}, event.get("end") or {}
    all_day = "date" in start
    out = {
        "id": event.get("id"),
        "summary": event.get("summary", ""),
        "all_day": all_day,
        "start": start.get("date") if all_day else start.get("dateTime"),
        "end": _last_day(end.get("date")) if all_day else end.get("dateTime"),
    }
    description = event.get("description")
    if description:
        description, cut = mailtext.limit(description, max_description)
        if cut:
            out["description_truncated"] = True
    event_type = event.get("eventType")
    optional = {
        "time_zone": start.get("timeZone"),
        "location": event.get("location"),
        "description": description,
        "status": event.get("status"),
        "organizer": (event.get("organizer") or {}).get("email"),
        "attendees": [{"email": a.get("email"), "response": a.get("responseStatus")}
                      for a in event.get("attendees") or []],
        "recurring_event_id": event.get("recurringEventId"),
        "event_type": None if event_type == "default" else event_type,
        "html_link": event.get("htmlLink") if link else None,
    }
    out.update({key: value for key, value in optional.items() if value})
    return out


def calendar_time_zone(svc, calendar_id: str) -> str:
    return svc.calendarList().get(calendarId=calendar_id, fields="timeZone").execute()["timeZone"]


def list_calendars(svc) -> list[dict]:
    """Every calendar in the account's list, following all pages."""
    calendars, token, seen = [], None, set()
    while True:
        if token in seen:
            raise RuntimeError("Calendar list pagination loop detected.")
        seen.add(token)
        resp = svc.calendarList().list(
            fields="nextPageToken,items(id,summary,summaryOverride,primary,accessRole,timeZone)",
            **({"pageToken": token} if token else {}),
        ).execute()
        for item in resp.get("items", []):
            calendars.append({
                "id": item.get("id"),
                "name": item.get("summaryOverride") or item.get("summary", ""),
                "primary": bool(item.get("primary")),
                "access": item.get("accessRole"),
                "time_zone": item.get("timeZone"),
            })
        token = resp.get("nextPageToken")
        if not token:
            return calendars


def list_events(svc, calendar_id: str, max_results: int, page_token: str | None = None, **params) -> dict:
    """Fill a bounded page of events in start order and return the continuation."""
    if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
        raise ValueError("max_results must be a positive integer.")
    events, token, seen, zone = [], page_token, set(), None
    while len(events) < max_results:
        if token in seen:
            raise RuntimeError("Calendar pagination loop detected.")
        seen.add(token)
        resp = svc.events().list(
            calendarId=calendar_id, singleEvents=True, orderBy="startTime",
            maxResults=min(2500, max_results - len(events)),
            fields=f"nextPageToken,timeZone,items({EVENT_FIELDS})",
            **({"pageToken": token} if token else {}), **params,
        ).execute()
        events.extend(resp.get("items", []))
        zone = zone or resp.get("timeZone")
        token = resp.get("nextPageToken")
        if not token:
            break
    return {
        "time_zone": zone,
        "events": [simplify(e, max_description=LIST_DESCRIPTION_CHARS, link=False) for e in events],
        "next_page_token": token,
    }


def get_event(svc, calendar_id: str, event_id: str) -> dict:
    return svc.events().get(calendarId=calendar_id, eventId=event_id, fields=EVENT_FIELDS).execute()
