"""Calendar in, calendar out. No LLM anywhere in this module.

Events are already structured data, so Gemini has nothing to interpret here --
this module just turns "what's on your calendar" into the same ``busy_block``
rows the Life Compiler produces, so the scheduler treats both identically:

    {"date": "2026-09-21", "start_time": "09:00", "end_time": "17:00", "label": "Busy"}

Three ways in / out:
  * Google Calendar (OAuth, read-only scope)  -> ``fetch_busy``
  * any .ics file (Google/Apple/Outlook export) -> ``parse_ics``  (no OAuth, offline)
  * study plan -> .ics                          -> ``sessions_to_ics``

Privacy: event titles are dropped by default (every block is labelled "Busy")
because block labels are forwarded to Gemini by the scheduler. Opt in per import.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]

# Read-only on purpose: DAP4Y never edits the student's real calendar.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
CREDENTIALS_PATH = Path(os.getenv("GOOGLE_OAUTH_CLIENT", REPO_ROOT / "credentials.json"))
TOKEN_PATH = Path(os.getenv("GOOGLE_TOKEN_PATH", REPO_ROOT / "data" / "gcal_token.json"))

_HHMM = re.compile(r"^\d{2}:\d{2}$")


class CalendarError(Exception):
    """Message is safe to show the student."""


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def local_tz() -> tzinfo:
    """``DAP4Y_TZ`` (e.g. America/Toronto) wins; else the machine's current offset."""
    name = os.getenv("DAP4Y_TZ")
    if name:
        try:
            return ZoneInfo(name)
        except Exception as exc:
            raise CalendarError(f"DAP4Y_TZ='{name}' is not a valid timezone.") from exc
    return datetime.now().astimezone().tzinfo or timezone.utc


def _window(days: int, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=days)


def _blocks(start: datetime, end: datetime, label: str, tz: tzinfo) -> list[dict]:
    """Split an aware [start, end) interval into one busy block per local day."""
    s, e = start.astimezone(tz), end.astimezone(tz)
    out: list[dict] = []
    cur = s
    while cur < e:
        day_end = datetime.combine(cur.date() + timedelta(days=1), time.min, tzinfo=tz)
        seg_end = min(e, day_end)
        out.append({
            "date": cur.date().isoformat(),
            "start_time": cur.strftime("%H:%M"),
            "end_time": "23:59" if seg_end == day_end else seg_end.strftime("%H:%M"),
            "label": label,
        })
        cur = seg_end
    return [b for b in out if b["start_time"] < b["end_time"]]


# --------------------------------------------------------------------------
# Google Calendar (live)
# --------------------------------------------------------------------------

def configured() -> bool:
    """True once the OAuth client file from Google Cloud Console is in place."""
    return CREDENTIALS_PATH.exists()


def _load_creds():
    from google.auth.transport.requests import Request  # noqa: PLC0415
    from google.oauth2.credentials import Credentials  # noqa: PLC0415

    if not TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception:
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds
        except Exception:
            return None
    return None


def connected() -> bool:
    try:
        return _load_creds() is not None
    except ImportError:
        return False


def connect() -> None:
    """Open the Google consent screen in a browser and store the token locally.

    Blocks until the student finishes (or abandons) the flow. Runs on the
    machine hosting Streamlit, which is fine for a local demo.
    """
    if not configured():
        raise CalendarError(f"No OAuth client file found at {CREDENTIALS_PATH}.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: PLC0415

        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    except ImportError as exc:
        raise CalendarError("Install google-auth-oauthlib and google-api-python-client.") from exc
    except Exception as exc:
        raise CalendarError(f"Google sign-in did not complete ({type(exc).__name__}).") from exc
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def disconnect() -> None:
    TOKEN_PATH.unlink(missing_ok=True)


def _service():
    creds = _load_creds()
    if creds is None:
        raise CalendarError("Google Calendar isn't connected (or the sign-in expired).")
    from googleapiclient.discovery import build  # noqa: PLC0415

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _api(call):
    from googleapiclient.errors import HttpError  # noqa: PLC0415

    try:
        return call.execute()
    except HttpError as exc:
        status = getattr(exc.resp, "status", "?")
        raise CalendarError(f"Google Calendar API error (HTTP {status}).") from exc


def list_calendars() -> list[dict]:
    """[{id, name, primary}] for every calendar the student can see."""
    items = _api(_service().calendarList().list(minAccessRole="reader")).get("items", [])
    return [
        {"id": c["id"], "name": c.get("summaryOverride") or c.get("summary", c["id"]),
         "primary": bool(c.get("primary"))}
        for c in items
    ]


def _parse_google_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_busy(days: int = 14, calendar_ids: list[str] | None = None,
               include_titles: bool = False) -> list[dict]:
    """Busy blocks for the next ``days`` days from the chosen Google calendars."""
    svc = _service()
    primary = _api(svc.calendars().get(calendarId="primary"))
    tz: tzinfo = ZoneInfo(primary["timeZone"]) if primary.get("timeZone") else local_tz()
    start, end = _window(days, tz)

    out: list[dict] = []
    for cal_id in calendar_ids or ["primary"]:
        token = None
        while True:
            resp = _api(svc.events().list(
                calendarId=cal_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                singleEvents=True, orderBy="startTime", maxResults=250, pageToken=token))
            for ev in resp.get("items", []):
                if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                    continue
                if ev.get("eventType") in {"workingLocation", "birthday"}:
                    continue
                me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
                if me and me.get("responseStatus") == "declined":
                    continue
                s, e = ev.get("start", {}), ev.get("end", {})
                if "dateTime" not in s or "dateTime" not in e:  # all-day events: skipped
                    continue
                label = (ev.get("summary") or "Busy") if include_titles else "Busy"
                out += _blocks(_parse_google_dt(s["dateTime"]), _parse_google_dt(e["dateTime"]),
                               label, tz)
            token = resp.get("nextPageToken")
            if not token:
                break
    return merge_busy(out)


# --------------------------------------------------------------------------
# .ics import (works offline, no OAuth)
# --------------------------------------------------------------------------

def parse_ics(data: bytes, days: int = 14, include_titles: bool = False,
              tz: tzinfo | None = None) -> list[dict]:
    """Busy blocks from an .ics export. Recurring events are expanded."""
    try:
        import icalendar  # noqa: PLC0415
        import recurring_ical_events  # noqa: PLC0415
    except ImportError as exc:
        raise CalendarError("Install icalendar and recurring-ical-events.") from exc

    tz = tz or local_tz()
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as exc:
        raise CalendarError("That doesn't look like a valid .ics calendar file.") from exc

    start, end = _window(days, tz)
    out: list[dict] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        if "DTSTART" not in ev:
            continue
        s = ev.decoded("DTSTART")
        if not isinstance(s, datetime):  # all-day: skipped
            continue
        if "DTEND" in ev:
            e = ev.decoded("DTEND")
        elif "DURATION" in ev:
            e = s + ev.decoded("DURATION")
        else:
            continue
        if not isinstance(e, datetime):
            continue
        s = s if s.tzinfo else s.replace(tzinfo=tz)  # floating time = local
        e = e if e.tzinfo else e.replace(tzinfo=tz)
        label = (str(ev.get("SUMMARY", "")) or "Busy") if include_titles else "Busy"
        out += _blocks(s, e, label, tz)
    return merge_busy(out)


# --------------------------------------------------------------------------
# combine + export
# --------------------------------------------------------------------------

def merge_busy(*lists: list[dict]) -> list[dict]:
    """Union of busy blocks: sorted, overlaps within a day collapsed.

    Blocks that aren't strict ISO date + HH:MM (e.g. something odd from the
    Life Compiler) are passed through untouched rather than dropped.
    """
    by_date: dict[str, list[dict]] = {}
    passthrough: list[dict] = []
    for lst in lists:
        for b in lst or []:
            ok = (re.match(r"^\d{4}-\d{2}-\d{2}$", str(b.get("date", "")))
                  and _HHMM.match(str(b.get("start_time", "")))
                  and _HHMM.match(str(b.get("end_time", ""))))
            (by_date.setdefault(b["date"], []) if ok else passthrough).append(b)

    out: list[dict] = []
    for d in sorted(by_date):
        cur: dict | None = None
        for b in sorted(by_date[d], key=lambda x: (x["start_time"], x["end_time"])):
            if cur and b["start_time"] <= cur["end_time"]:
                cur["end_time"] = max(cur["end_time"], b["end_time"])
                lab = b.get("label", "")
                if lab and lab not in cur.get("label", "").split(" + "):
                    cur["label"] = f"{cur.get('label', '')} + {lab}".strip(" +")[:80]
            else:
                cur = {"date": d, "start_time": b["start_time"], "end_time": b["end_time"],
                       "label": b.get("label", "Busy")}
                out.append(cur)
    return out + passthrough


def sessions_to_ics(sessions: list[dict], tz: tzinfo | None = None,
                    name: str = "DAP4Y study plan") -> bytes:
    """Planned study blocks -> .ics. UIDs are stable, so re-importing updates
    events in Google/Apple Calendar instead of duplicating them."""
    import icalendar  # noqa: PLC0415

    tz = tz or local_tz()
    cal = icalendar.Calendar()
    cal.add("prodid", "-//DAP4Y//Study plan//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    for s in sessions:
        if s.get("status") == "skipped":
            continue
        try:
            start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}").replace(tzinfo=tz)
            end = datetime.fromisoformat(f"{s['date']}T{s['end_time']}").replace(tzinfo=tz)
        except (KeyError, ValueError):
            continue
        ev = icalendar.Event()
        ev.add("uid", f"dap4y-session-{s.get('id', start.isoformat())}@dap4y")
        ev.add("dtstamp", datetime.now(timezone.utc))
        ev.add("dtstart", start)
        ev.add("dtend", end)
        code = s.get("course_code")
        ev.add("summary", f"[{code}] {s.get('focus', 'Study')}" if code else s.get("focus", "Study"))
        if s.get("rationale"):
            ev.add("description", s["rationale"])
        cal.add_component(ev)
    return cal.to_ical()