"""UofT Index client -- the ONLY external source of course difficulty.

Why this module exists instead of handing Gemini a browsing tool:

  uoftindex.ca is a client-rendered SPA. Fetching the course URL server-side
  returns an empty JavaScript shell with zero course data, so Gemini's
  ``url_context`` tool (and any plain HTTP fetch of the page) comes back with
  nothing. The real data lives behind the site's own GraphQL endpoint.

  Fetching it ourselves is also a *stricter* reading of "only consult
  uoftindex.ca" than giving Gemini a web tool would be: the model receives one
  JSON payload from one origin and has no ability to reach anywhere else.

The endpoint rejects requests that do not carry BOTH ``Origin`` and ``Referer``
(either one alone returns a generic error), which is why they are set below.

Results are cached on disk so a demo does not hammer a volunteer-run site.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://uoftindex.ca/graphql"
SITE = "https://uoftindex.ca"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache" / "uoftindex"
CACHE_TTL = 7 * 24 * 3600  # course metrics change at most once a term
TIMEOUT = 20

# UofT course codes: 3 letters, 3 digits, H (half) or Y (full), campus digit.
CODE_RE = re.compile(r"^([A-Z]{3})(\d{3})([HY])(\d)$")
STEM_RE = re.compile(r"^([A-Z]{3})\s*(\d{3})$")
# St. George, Mississauga, Scarborough -- half credit first, then full year.
SUFFIXES = ("H1", "H5", "H3", "Y1", "Y5", "Y3")

Q_COURSE_INFO = (
    "query courseInfo($course: String!) { courseInfo(course: $course) { "
    "code, course, description, bird, drop, prereq, coreq, exclusion, limits, "
    "breadth, distribution, rating, recommend, workload, understanding, "
    "evaluations, environment, application, stimulating, numReviews, insights } }"
)
Q_TIMETABLE = (
    "query getTimetable($course: String!, $sem: String, $year: String) { "
    "getTimetable(course: $course, sem: $sem, year: $year) { timetable, filters } }"
)
Q_HISTORY = (
    "query getHistory($filter: JSON!, $limit: Float!) { "
    "getHistory(filter: $filter, limit: $limit) { history } }"
)


class LookupError_(Exception):
    """Raised when UofT Index cannot be reached or has no such course."""


def _headers(code: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Origin": SITE,
        "Referer": f"{SITE}/courses?c={code}",
    }


def _post(operation: str, query: str, variables: dict, code: str) -> dict:
    body = json.dumps(
        {"operationName": operation, "query": query, "variables": variables}
    ).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers=_headers(code))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LookupError_(f"UofT Index unreachable: {type(exc).__name__}") from exc
    if payload.get("errors"):
        msg = payload["errors"][0].get("message", "unknown GraphQL error")
        raise LookupError_(f"UofT Index rejected the query: {msg}")
    return payload.get("data") or {}


# --------------------------------------------------------------------------
# course code handling
# --------------------------------------------------------------------------

def candidates(raw: str) -> list[str]:
    """Turn whatever the syllabus said into UofT code candidates to try.

    'CSC373H1' -> ['CSC373H1']      (already exact)
    'CSC 373'  -> ['CSC373H1', 'CSC373H5', ...]  (campus unknown, try each)
    """
    cleaned = re.sub(r"[\s\-_]", "", (raw or "")).upper()
    if CODE_RE.match(cleaned):
        return [cleaned]
    stem = STEM_RE.match(cleaned)
    if stem:
        return [f"{stem.group(1)}{stem.group(2)}{s}" for s in SUFFIXES]
    return []


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

def _cache_path(code: str) -> Path:
    return CACHE_DIR / f"{code}.json"


def _cached(code: str) -> dict | None:
    p = _cache_path(code)
    if not p.exists() or time.time() - p.stat().st_mtime > CACHE_TTL:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _store(code: str, data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(code).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # cache is an optimisation, never a requirement


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def fetch(raw_code: str, use_cache: bool = True) -> dict:
    """Everything UofT Index knows about one course.

    Returns ``{code, info, timetable, history, source}``. Raises
    ``LookupError_`` if the course cannot be found on any campus.
    """
    tried = candidates(raw_code)
    if not tried:
        raise LookupError_(
            f"'{raw_code}' is not a UofT course code (expected e.g. CSC373H1)")

    last: Exception | None = None
    for code in tried:
        if use_cache:
            hit = _cached(code)
            if hit:
                return {**hit, "source": "cache"}
        try:
            data = _post("courseInfo", Q_COURSE_INFO, {"course": code}, code)
        except LookupError_ as exc:
            last = exc
            continue
        info_list = data.get("courseInfo") or []
        if not info_list:
            continue  # wrong campus suffix, try the next
        info = info_list[0]

        bundle = {"code": code, "info": info, "timetable": None, "history": None}
        # these two are nice-to-have; a failure must not lose the info we got
        try:
            tt = _post("getTimetable", Q_TIMETABLE,
                       {"course": code, "sem": None, "year": None}, code)
            bundle["timetable"] = (tt.get("getTimetable") or {}).get("timetable")
        except LookupError_:
            pass
        try:
            h = _post("getHistory", Q_HISTORY,
                      {"filter": {"code": code}, "limit": 10}, code)
            bundle["history"] = (h.get("getHistory") or {}).get("history")
        except LookupError_:
            pass

        _store(code, bundle)
        return {**bundle, "source": "uoftindex.ca"}

    raise LookupError_(
        last.args[0] if last else f"No UofT Index entry for '{raw_code}' "
        f"(tried {', '.join(tried)})")


def difficulty_signals(bundle: dict) -> dict:
    """The subset of the payload that actually speaks to difficulty.

    Keeping this small matters: it is what gets sent to Gemini, and a tight
    payload is both cheaper and less prone to the model latching onto noise.
    """
    info = bundle.get("info") or {}
    return {
        "code": info.get("code"),
        "title": info.get("course"),
        "drop_rate_pct": info.get("drop"),
        "bird_count": info.get("bird"),
        "overall_rating_5": info.get("rating"),
        "workload_5": info.get("workload"),
        "understanding_5": info.get("understanding"),
        "evaluations_5": info.get("evaluations"),
        "stimulating_5": info.get("stimulating"),
        "recommend_5": info.get("recommend"),
        "review_count": info.get("numReviews"),
        "prerequisites": info.get("prereq"),
        "exclusions": info.get("exclusion"),
        "enrolment_limits": info.get("limits"),
        "drop_history": bundle.get("history"),
    }


def class_times(bundle: dict, sem: str | None = None,
                year: str | None = None) -> list[dict]:
    """Lecture and tutorial slots, as weekly busy blocks.

    This is what makes "after-university study schedule" mean something: we
    know when the student is physically in class, so nothing gets planned there.
    """
    tt = bundle.get("timetable") or {}
    out: list[dict] = []
    for kind in ("lec", "tut", "pra"):
        for section in tt.get(kind) or []:
            if sem and section.get("sem") != sem:
                continue
            if year and str(section.get("year")) != str(year):
                continue
            for slot in section.get("times") or []:
                start, _, end = (slot.get("time") or "").partition(" - ")
                if not start or not end:
                    continue
                out.append({
                    "course_code": bundle.get("code"),
                    "kind": kind,
                    "section": section.get("section"),
                    "day": (slot.get("day") or "").upper()[:3],
                    "start_time": _to_24h(start),
                    "end_time": _to_24h(end),
                    "location": (slot.get("location") or {}).get("room", ""),
                })
    return out


def _to_24h(value: str) -> str:
    """'11:00 AM' -> '11:00';  '1:00 PM' -> '13:00'."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?\s*$", value or "")
    if not m:
        return (value or "").strip()
    hour, minute, half = int(m.group(1)), m.group(2), m.group(3).upper()
    if half == "P" and hour != 12:
        hour += 12
    elif half == "A" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"
