"""Offline fallbacks for every Gemini call.

These are regex/heuristic parsers, not canned answers -- they genuinely try to
read the input, just far worse than Gemini does. That keeps the demo honest
when the API key is missing or the venue wifi dies mid-pitch.

Every function here returns the same shape as its ``gemini.py`` counterpart.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1
    )
}
for _m, _i in list(MONTHS.items()):
    MONTHS[_m[:3]] = _i

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

KIND_WORDS = {
    "final": "final", "midterm": "midterm", "exam": "midterm", "test": "quiz",
    "quiz": "quiz", "project": "project", "lab": "lab", "assignment": "assignment",
    "homework": "assignment", "problem set": "assignment", "pset": "assignment",
    "essay": "assignment", "report": "assignment", "presentation": "project",
}

# Lines that mention a weight or a date but are rules, not graded items.
POLICY_RE = re.compile(
    r"\b(late policy|penalt\w*|per day|collaboration|academic integrity|"
    r"plagiaris\w*|office hours?|prerequisite|attendance|drop date|withdraw\w*)\b", re.I)


def _today(today: str = "") -> date:
    try:
        return date.fromisoformat(today)
    except (ValueError, TypeError):
        return date.today()


def _find_date(line: str, base: date) -> tuple[str | None, int]:
    """Pull an ISO date out of free text. Returns (iso, index where it starts)."""
    m = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", line)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return date(y, mo, d).isoformat(), m.start()
        except ValueError:
            return None, -1

    # scan every "<word> <number>" pair -- "Set 2" must not shadow "November 6"
    for m in re.finditer(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(20\d{2}))?\b", line
    ):
        if m.group(1).lower() not in MONTHS:
            continue
        mo = MONTHS[m.group(1).lower()]
        d = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else base.year
        try:
            return date(y, mo, d).isoformat(), m.start()
        except ValueError:
            continue

    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", line)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or base.year)
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d).isoformat(), m.start()
        except ValueError:
            return None, -1

    for i, name in enumerate(WEEKDAYS):
        m = re.search(rf"\b(next\s+)?{name}\b", line, re.I)
        if m:
            ahead = (i - base.weekday()) % 7 or 7
            return (base + timedelta(days=ahead)).isoformat(), m.start()
    return None, -1


def _date_of(line: str, base: date) -> str | None:
    return _find_date(line, base)[0]


def _kind(line: str) -> str:
    low = line.lower()
    for word, kind in KIND_WORDS.items():
        if word in low:
            return kind
    return "assignment"


def _title(line: str, date_pos: int = -1) -> str:
    """Strip the bullet, the weight parenthetical and the trailing date clause."""
    cut = len(line)
    if date_pos >= 0:
        cut = date_pos
    m = re.search(r"\b(due|on|by|submitted)\b", line[:cut], re.I)
    if m and m.start() > 4:
        cut = m.start()

    cleaned = line[:cut]
    cleaned = re.sub(r"^\s*[-*•–]\s*", "", cleaned)          # leading bullet only
    cleaned = re.sub(r"\(?\s*\d+(\.\d+)?\s*%\s*\)?", "", cleaned)      # the weight
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" .,:;-–")
    # drop a dangling connector left behind by the cut ("... 10 - every")
    cleaned = re.sub(r"\s+(from|every|on|at|in|by|the|starting|beginning)$", "",
                     cleaned, flags=re.I).strip(" .,:;-–")
    return cleaned[:80] or "Untitled item"


# --------------------------------------------------------------------------

def parse_syllabus(text: str, today: str = "") -> dict:
    base = _today(today)
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    code = ""
    m = re.search(r"\b([A-Z]{2,5})\s?-?\s?(\d{3,4}[A-Z]?)\b", text or "")
    if m:
        code = f"{m.group(1)} {m.group(2)}"
    name = ""
    for ln in lines[:8]:
        if code and code.replace(" ", "") in ln.replace(" ", "").replace("-", ""):
            tail = re.split(r"[-–:]", ln, maxsplit=1)
            if len(tail) > 1 and len(tail[1].strip()) > 3:
                name = tail[1].strip()
                break
    if not name and lines:
        name = lines[0]
    # "Algorithms (Fall 2026)" -> "Algorithms"; drop a leading course code too
    name = re.sub(r"\s*[-–,(]?\s*\b(?:fall|winter|spring|summer)\b[^)]*\)?\s*$",
                  "", name, flags=re.I)
    if code:
        name = re.sub(rf"^\s*{re.escape(code)}\s*[-–:]?\s*", "", name, flags=re.I)
    name = name.strip(" -–:")[:60]

    instructor = ""
    m = re.search(r"(?:instructor|professor|prof\.?|lecturer|taught by)\s*[:\-]?\s*(.+)",
                  text or "", re.I)
    if m:
        instructor = m.group(1).strip().splitlines()[0][:60]

    textbook = ""
    m = re.search(r"(?:textbook|text|required reading|course text)\s*[:\-]?\s*(.+)",
                  text or "", re.I)
    if m:
        textbook = m.group(1).strip().splitlines()[0][:120]

    assessments, topics, warnings = [], [], []
    for ln in lines:
        pct = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", ln)
        due, due_pos = _find_date(ln, base)
        looks_graded = any(w in ln.lower() for w in KIND_WORDS)
        if (pct or due) and looks_graded and not POLICY_RE.search(ln):
            assessments.append({
                "title": _title(ln, due_pos),
                "kind": _kind(ln),
                "due_date": due or "",
                "weight_pct": float(pct.group(1)) if pct else 0.0,
                "notes": "" if pct else "no weight found in source",
            })
        m = re.match(r"(?:week|wk)\s*(\d{1,2})\s*[:\-–]?\s*(.+)", ln, re.I)
        if m:
            topics.append({
                "name": m.group(2).strip()[:80],
                "week": int(m.group(1)),
                "summary": "",
            })

    total = sum(a["weight_pct"] for a in assessments)
    if assessments and abs(total - 100) > 1:
        warnings.append(f"Weights sum to {total:g}%, not 100% -- check for missed items.")
    if not assessments:
        warnings.append("No graded items recognised. Offline parser needs dated lines.")
    warnings.append("Parsed offline without Gemini -- verify everything.")

    difficulty = 3
    if len(assessments) >= 6 or total > 0 and any(a["weight_pct"] >= 40 for a in assessments):
        difficulty = 4
    return {
        "course": {
            "code": code or "UNKNOWN",
            "name": name,
            "instructor": instructor,
            "textbook": textbook,
            "credits": 3,
            "difficulty": difficulty,
            "difficulty_reason": f"{len(assessments)} graded items detected (offline heuristic).",
        },
        "assessments": assessments,
        "topics": topics,
        "warnings": warnings,
    }


def parse_notes(text: str, course_code: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    headings = [ln for ln in lines if re.match(r"^(#{1,3}\s|\d+[.)]\s|[A-Z][A-Za-z ]{3,40}:$)", ln)]
    topics = []
    for h in headings[:6] or lines[:3]:
        topics.append({
            "name": re.sub(r"^(#{1,3}\s*|\d+[.)]\s*)", "", h).strip(" :")[:70],
            "source": "tutorial" if "tutorial" in (text or "").lower() else "lecture",
            "week": 0,
            "summary": "",
            "keywords": [],
        })
    if not topics:
        topics = [{"name": f"{course_code} notes", "source": "lecture", "week": 0,
                   "summary": (text or "")[:180], "keywords": []}]

    confusions = [
        re.sub(r"\s{2,}", " ", re.sub(r"^\s*[-*•]\s*", "", ln)).strip(" .,-")[:100]
        for ln in lines
        if "??" in ln or re.search(r"\b(ask prof|unclear|confus\w*|not sure|revisit)\b", ln, re.I)
    ]

    todos = []
    for t in topics:
        todos.append({
            "title": f"Re-derive '{t['name']}' from a blank page",
            "detail": "Close the notes, reconstruct the core result, then diff against them.",
            "topic": t["name"], "est_minutes": 40, "kind": "review",
        })
        todos.append({
            "title": f"Work 3 problems on {t['name']}",
            "detail": "Pick from the tutorial set; time-box each to 10 minutes.",
            "topic": t["name"], "est_minutes": 45, "kind": "practice",
        })
    for c in confusions[:3]:
        todos.append({
            "title": f"Resolve: {c[:60]}",
            "detail": "Flagged as confusing in your own notes. Office hours or textbook.",
            "topic": topics[0]["name"], "est_minutes": 25, "kind": "clarify",
        })
    return {
        "course_code": course_code,
        "topics": topics,
        "todos": todos,
        "flagged_confusions": confusions,
    }


def parse_intent(text: str, today: str = "") -> dict:
    base = _today(today)
    busy, deadlines = [], []
    for ln in re.split(r"[.,;\n]", text or ""):
        ln = ln.strip()
        if not ln:
            continue
        d, pos = _find_date(ln, base)
        if not d:
            continue
        if re.search(r"\b(exam|midterm|final|due|deadline|submit|quiz|test)\b", ln, re.I):
            deadlines.append({"course_code": "", "title": _title(ln, pos),
                              "date": d, "kind": _kind(ln)})
        elif re.search(r"\b(work|shift|class|lecture|lab|practice|meeting|busy)\b", ln, re.I):
            busy.append({"date": d, "start_time": "09:00", "end_time": "17:00",
                         "label": _title(ln, pos)})

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", text or "", re.I)
    daily = int(float(m.group(1)) * 60) if m else 120
    windows = [w for w in ("morning", "afternoon", "evening", "night")
               if re.search(rf"\b{w}", text or "", re.I)]
    return {
        "busy_blocks": busy,
        "deadlines": deadlines,
        "preferences": {
            "daily_minutes": daily,
            "preferred_windows": windows or ["evening"],
            "session_minutes": 90,
            "notes": "Parsed offline without Gemini.",
        },
        "echo": f"Read {len(deadlines)} deadline(s) and {len(busy)} busy block(s) offline.",
    }


def generate_questions(course_code: str, topic_name: str, n: int = 5) -> dict:
    stems = [
        ("State the central result of {t} precisely, including every hypothesis.",
         "A correct statement names the assumptions; dropping one is the usual error.", 2),
        ("Work a concrete example of {t} end to end and check your answer.",
         "Any worked instance with the steps shown and a sanity check at the end.", 3),
        ("Where does {t} break down? Give a case it does NOT handle and say why.",
         "Name a violated hypothesis and show the failure it causes.", 4),
        ("Explain {t} to a classmate who missed the lecture, in under 100 words.",
         "Plain-language account that keeps the mechanism, not just the name.", 2),
        ("Compare {t} to the method from the previous topic: when would you pick each?",
         "A decision rule based on cost, assumptions or the shape of the input.", 4),
        ("Derive {t} from first principles without consulting your notes.",
         "Full derivation; the model answer is your own notes' derivation.", 5),
    ]
    out = []
    for i in range(max(1, n)):
        stem, ans, diff = stems[i % len(stems)]
        out.append({
            "prompt": stem.format(t=topic_name),
            "answer": ans,
            "kind": "short",
            "options": [],
            "difficulty": diff,
            "targets": topic_name,
        })
    return {"questions": out}


def grade_attempts(items: list[dict], is_retake: bool = False) -> dict:
    """Offline grading is a keyword-overlap proxy and says so loudly."""
    results = []
    for it in items:
        ans = (it.get("student_answer") or "").strip()
        model = (it.get("answer") or "")
        if not ans:
            results.append({"question_id": it.get("question_id", 0), "score": 0.0,
                            "verdict": "blank", "feedback": "No answer submitted.",
                            "gaps": ["unattempted"]})
            continue
        key = {w for w in re.findall(r"[a-z]{5,}", model.lower())}
        got = {w for w in re.findall(r"[a-z]{5,}", ans.lower())}
        overlap = len(key & got) / len(key) if key else 0.0
        length_ok = min(len(ans.split()) / 40, 1.0)
        score = round(min(1.0, 0.55 * overlap + 0.45 * length_ok), 2)
        verdict = "correct" if score >= 0.75 else "partial" if score >= 0.4 else "incorrect"
        results.append({
            "question_id": it.get("question_id", 0),
            "score": score,
            "verdict": verdict,
            "feedback": ("Offline scoring: keyword overlap with the model answer only. "
                         "Set GEMINI_API_KEY for real marking."),
            "gaps": [] if score >= 0.75 else ["needs-real-grading"],
        })
    avg = sum(r["score"] for r in results) / len(results) if results else 0.0
    return {
        "results": results,
        "overall": (f"{'Retake' if is_retake else 'First attempt'}: {avg:.0%} by offline "
                    "keyword proxy. Not a real grade."),
    }


def diagnose(snapshot: dict) -> dict:
    weak = sorted(snapshot.get("mastery", []), key=lambda m: m.get("score", 1))[:5]
    struggles = [{
        "label": m.get("label", "unknown"),
        "course_code": m.get("course_code", ""),
        "severity": "high" if m.get("score", 1) < 0.4 else "medium",
        "evidence": f"Mastery {m.get('score', 0):.0%} over {m.get('samples', 0)} attempt(s).",
        "fix": "Redo the lowest-scoring question cold, then compare to the model answer.",
    } for m in weak if m.get("score", 1) < 0.7]
    strong = [m.get("label", "") for m in snapshot.get("mastery", [])
              if m.get("score", 0) >= 0.8][:4]
    return {
        "struggles": struggles,
        "strengths": strong,
        "coach_note": "Offline diagnosis: ranked by stored mastery scores only.",
    }


def recommend_resources(struggles: list[dict], courses: list[dict]) -> dict:
    books = {c.get("code"): c.get("textbook", "") for c in courses}
    out = []
    for s in struggles[:6]:
        code = s.get("course_code", "")
        book = books.get(code) or "your course textbook"
        out.append({
            "label": s.get("label", ""),
            "course_code": code,
            "title": f"{book} -- section on {s.get('label', 'this topic')}",
            "kind": "reading",
            "locator": f"index lookup: {s.get('label', '')}",
            "why": "Closest authoritative source you already own.",
        })
        out.append({
            "label": s.get("label", ""),
            "course_code": code,
            "title": f"Practice set: {s.get('label', '')}",
            "kind": "practice",
            "locator": f"search: \"{code} {s.get('label', '')} practice problems solutions\"",
            "why": "Offline suggestion -- spaced practice beats rereading.",
        })
    return {"resources": out}


def find_overlaps(courses: list[dict], topics: list[dict]) -> dict:
    """Token-overlap between topic names across different courses."""
    stop = {"the", "and", "for", "with", "introduction", "basic", "advanced", "part",
            "theory", "theorem", "review", "methods", "notes", "week", "analysis",
            "problems", "applications", "overview"}
    by_course: dict[str, set[str]] = {}
    for t in topics:
        code = t.get("course_code", "")
        words = {w for w in re.findall(r"[a-z]{4,}", (t.get("name", "")).lower()) if w not in stop}
        by_course.setdefault(code, set()).update(words)

    seen, out = set(), []
    codes = list(by_course)
    for i, a in enumerate(codes):
        for b in codes[i + 1:]:
            for word in by_course[a] & by_course[b]:
                if word in seen:
                    continue
                seen.add(word)
                out.append({
                    "label": word,
                    "course_codes": [a, b],
                    "payoff": f"'{word}' appears in both {a} and {b} -- one deep session covers both.",
                    "saved_minutes": 45,
                })
    return {"overlaps": out[:8]}


def plan_week(state: dict) -> dict:
    """Greedy first-fit: walk forward from today, drop TODOs into free slots."""
    base = _today(state.get("today", ""))
    prefs = state.get("preferences", {})
    daily = int(prefs.get("daily_minutes", 180))
    block = int(prefs.get("session_minutes", 90))
    busy = state.get("busy_blocks", [])
    todos = list(state.get("todos", []))
    horizon = int(state.get("horizon_days", 7))

    busy_by_date: dict[str, list[tuple[int, int]]] = {}
    for b in busy:
        busy_by_date.setdefault(b.get("date", ""), []).append(
            (_mins(b.get("start_time", "09:00")), _mins(b.get("end_time", "17:00")))
        )

    sessions, idx = [], 0
    for day in range(horizon):
        if idx >= len(todos):
            break
        d = (base + timedelta(days=day)).isoformat()
        cursor, used = _mins(prefs.get("day_start", "09:00")), 0
        day_end = _mins(prefs.get("day_end", "22:00"))
        while idx < len(todos) and used + block <= daily and cursor + block <= day_end:
            clash = next((e for s, e in busy_by_date.get(d, []) if s < cursor + block and cursor < e), None)
            if clash is not None:
                cursor = clash
                continue
            t = todos[idx]
            sessions.append({
                "date": d,
                "start_time": _hhmm(cursor),
                "end_time": _hhmm(cursor + block),
                "course_code": t.get("course_code", ""),
                "focus": t.get("title", ""),
                "rationale": f"Priority {t.get('priority', 0):.0f}: {t.get('priority_why', '')}",
                "todo_titles": [t.get("title", "")],
            })
            cursor += block + int(prefs.get("break_minutes", 15))
            used += block
            idx += 1
    return {
        "sessions": sessions,
        "constraints_understood": [f"{daily} min/day", f"{block} min blocks",
                                   f"{len(busy)} busy block(s)", f"{horizon}-day horizon"],
        "tradeoffs": ([f"{len(todos) - idx} TODO(s) did not fit in {horizon} days."]
                      if idx < len(todos) else []) + ["Offline greedy scheduler."],
    }


def _mins(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 9 * 60


def _hhmm(total: int) -> str:
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
