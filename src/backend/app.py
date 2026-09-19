"""Service layer -- the only thing the Streamlit frontend is allowed to import.

Flow of the whole product, in one file:

    messy input --[gemini]--> structured rows --[scheduler]--> ranked plan
                                     |
                                attempts --[gemini]--> gaps --> mastery
                                     |                            |
                                     +--------> diagnosis, resources, overlap
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from . import db, gemini, scheduler, uoftindex
from .gemini import Attachment

COLOURS = ["#6c8ebf", "#b85450", "#82b366", "#9673a6", "#d79b00", "#3d8b8b", "#c06c84"]

# Gap tags that describe the submission, not the student's understanding.
NON_GAPS = {"unattempted", "needs-real-grading", "blank", "no answer", "n/a", "none"}


def bootstrap() -> None:
    db.init_db()


def today_iso() -> str:
    return date.today().isoformat()


# --------------------------------------------------------------------------
# student profile
# --------------------------------------------------------------------------

def get_student() -> dict:
    row = db.query_one("SELECT * FROM student WHERE id = 1") or {}
    row["preferred_windows"] = db.jload(row.get("preferred_windows"), [])
    return row


def save_student(**fields) -> None:
    if "preferred_windows" in fields:
        fields["preferred_windows"] = db.jdump(fields["preferred_windows"])
    db.update("student", 1, fields)


# --------------------------------------------------------------------------
# courses
# --------------------------------------------------------------------------

def list_courses() -> list[dict]:
    return db.query("SELECT * FROM course ORDER BY code")


def get_course(course_id: int) -> dict | None:
    return db.query_one("SELECT * FROM course WHERE id = ?", [course_id])


def course_by_code(code: str) -> dict | None:
    if not code:
        return None
    return db.query_one("SELECT * FROM course WHERE code = ? COLLATE NOCASE", [code.strip()])


def set_difficulty(course_id: int, value: int) -> None:
    """The blackboard's 'manual diff change' -- student overrides Gemini's guess."""
    db.update("course", course_id, {"difficulty": int(value), "difficulty_source": "manual"})
    recompute_priorities()


def delete_course(course_id: int) -> None:
    db.delete("course", course_id)


def _upsert_course(payload: dict) -> int:
    code = (payload.get("code") or "UNKNOWN").strip()
    existing = course_by_code(code)
    data = {
        "code": code,
        "name": (payload.get("name") or "").strip(),
        "instructor": (payload.get("instructor") or "").strip(),
        "textbook": (payload.get("textbook") or "").strip(),
        "credits": float(payload.get("credits") or 3),
    }
    if existing:
        # never clobber a difficulty the student set by hand
        if existing["difficulty_source"] != "manual":
            data["difficulty"] = int(payload.get("difficulty") or 3)
        db.update("course", existing["id"], data)
        return existing["id"]
    data["difficulty"] = int(payload.get("difficulty") or 3)
    data["colour"] = COLOURS[len(list_courses()) % len(COLOURS)]
    return db.insert("course", data)


# --------------------------------------------------------------------------
# Part 1 -- ingestion
# --------------------------------------------------------------------------

def ingest_syllabus(text: str, attachments: list[Attachment] | None = None,
                    filename: str = "") -> dict:
    """Syllabus / portal screenshots -> course + every deadline + weightage + TODOs."""
    student = get_student()
    payload, source = gemini.parse_syllabus(
        text, attachments, today=today_iso(), term=student.get("term", "")
    )

    course_id = _upsert_course(payload.get("course", {}))
    course = get_course(course_id)

    created = {"assessments": 0, "topics": 0, "todos": 0}
    for a in payload.get("assessments", []):
        aid = db.insert("assessment", {
            "course_id": course_id,
            "title": (a.get("title") or "Untitled")[:120],
            "kind": a.get("kind") or "assignment",
            "due_date": a.get("due_date") or None,
            "weight_pct": float(a.get("weight_pct") or 0),
            "notes": a.get("notes") or "",
        })
        created["assessments"] += 1
        db.insert("todo", {
            "course_id": course_id,
            "assessment_id": aid,
            "title": f"Prepare: {a.get('title')}",
            "detail": f"Auto-created from the syllabus. {a.get('notes') or ''}".strip(),
            "est_minutes": 90 if a.get("kind") in ("midterm", "final", "project") else 60,
            "due_date": a.get("due_date") or None,
        })
        created["todos"] += 1

    for t in payload.get("topics", []):
        db.insert("topic", {
            "course_id": course_id,
            "name": (t.get("name") or "Untitled")[:120],
            "source": "lecture",
            "week": int(t.get("week") or 0),
            "summary": t.get("summary") or "",
        })
        created["topics"] += 1

    db.insert("ingest_log", {
        "kind": "syllabus",
        "filename": filename,
        "summary": (f"{course['code']}: {created['assessments']} assessments, "
                    f"{created['topics']} topics [{source}]"),
        "payload": db.jdump(payload),
    })
    recompute_priorities()
    return {"course": course, "created": created, "source": source,
            "warnings": payload.get("warnings", []), "raw": payload}


def ingest_notes(course_id: int, text: str, attachments: list[Attachment] | None = None,
                 filename: str = "", understanding_context: str = "",
                 energy_level: int | None = None) -> dict:
    """Lecture/tutorial notes -> topics + the TODOs that fill a study session."""
    course = get_course(course_id)
    if not course:
        raise ValueError("unknown course")

    payload, source = gemini.parse_notes(
        text,
        course["code"],
        attachments,
        today=today_iso(),
        understanding_context=understanding_context,
        energy_level=energy_level,
    )

    topic_ids: dict[str, int] = {}
    for t in payload.get("topics", []):
        name = (t.get("name") or "Untitled")[:120]
        existing = db.query_one(
            "SELECT id FROM topic WHERE course_id = ? AND name = ? COLLATE NOCASE",
            [course_id, name],
        )
        if existing:
            tid = existing["id"]
            db.update("topic", tid, {
                "summary": t.get("summary") or "",
                "keywords": db.jdump(t.get("keywords") or []),
                "source": t.get("source") or "lecture",
            })
        else:
            tid = db.insert("topic", {
                "course_id": course_id,
                "name": name,
                "source": t.get("source") or "lecture",
                "week": int(t.get("week") or 0),
                "summary": t.get("summary") or "",
                "keywords": db.jdump(t.get("keywords") or []),
            })
        topic_ids[name.lower()] = tid

    for td in payload.get("todos", []):
        db.insert("todo", {
            "course_id": course_id,
            "topic_id": topic_ids.get((td.get("topic") or "").lower()),
            "title": (td.get("title") or "Study")[:160],
            "detail": td.get("detail") or "",
            "est_minutes": int(td.get("est_minutes") or 45),
        })

    # a confusion the student wrote down is the earliest possible weakness signal
    for c in payload.get("flagged_confusions", []):
        _bump_mastery(course_id, c[:60], 0.3, topic_id=None)

    db.insert("ingest_log", {
        "kind": "notes",
        "filename": filename,
        "summary": (f"{course['code']}: {len(payload.get('topics', []))} topics, "
                    f"{len(payload.get('todos', []))} TODOs [{source}]"),
        "payload": db.jdump(payload),
    })
    recompute_priorities()
    return {"created": payload, "source": source}


def ingest_intent(text: str) -> dict:
    """'Life Compiler': one sentence of plain English -> constraints + deadlines."""
    payload, source = gemini.parse_intent(text, today=today_iso())

    prefs = payload.get("preferences", {})
    updates: dict[str, Any] = {}
    if prefs.get("daily_minutes"):
        updates["daily_capacity_min"] = int(prefs["daily_minutes"])
    if prefs.get("session_minutes"):
        updates["session_minutes"] = int(prefs["session_minutes"])
    if prefs.get("preferred_windows"):
        updates["preferred_windows"] = db.jdump(prefs["preferred_windows"])
    if updates:
        db.update("student", 1, updates)

    added = 0
    for d in payload.get("deadlines", []):
        course = course_by_code(d.get("course_code", ""))
        if not course:
            continue
        db.insert("assessment", {
            "course_id": course["id"],
            "title": d.get("title") or "Mentioned deadline",
            "kind": d.get("kind") or "assignment",
            "due_date": d.get("date") or None,
            "weight_pct": 0,
            "notes": "Added from a plain-language description; weight unknown.",
        })
        added += 1

    db.insert("ingest_log", {
        "kind": "intent",
        "filename": "",
        "summary": f"{payload.get('echo', '')} [{source}]",
        "payload": db.jdump(payload),
    })
    recompute_priorities()
    return {"payload": payload, "source": source, "deadlines_added": added}


# --------------------------------------------------------------------------
# PRE-PLANNING STAGE
#
#   syllabus -> course rows
#   course   -> uoftindex.ca -> Gemini -> difficulty + weekly hours
#   calendar + class times -> busy blocks
#   all of the above -> Gemini -> EMPTY study blocks
#
# Populating those blocks with actual tasks is a SEPARATE stage (owned by
# another teammate) and deliberately does not happen here.
# --------------------------------------------------------------------------

def lookup_difficulty(course_id: int, use_cache: bool = True) -> dict:
    """Cross-reference one course against uoftindex.ca and score its difficulty.

    Never overwrites a difficulty the student set by hand.
    """
    course = get_course(course_id)
    if not course:
        raise ValueError("unknown course")

    try:
        bundle = uoftindex.fetch(course["code"], use_cache=use_cache)
    except uoftindex.LookupError_ as exc:
        return {"ok": False, "course": course, "error": str(exc),
                "source": "uoftindex", "signals": None}

    signals = uoftindex.difficulty_signals(bundle)
    verdict, source = gemini.assess_difficulty(bundle["code"], signals)

    info = bundle.get("info") or {}
    fields = {
        "uoft_code": bundle["code"],
        "uoft_drop_rate": info.get("drop"),
        "uoft_workload": info.get("workload"),
        "uoft_rating": info.get("rating"),
        "uoft_reviews": info.get("numReviews"),
        "uoft_confidence": verdict.get("confidence") or "",
        "uoft_reasoning": verdict.get("reasoning") or "",
        "weekly_study_min": int(float(verdict.get("weekly_study_hours") or 0) * 60),
    }
    if course["difficulty_source"] != "manual":
        fields["difficulty"] = max(1, min(5, int(verdict.get("difficulty") or 3)))
        fields["difficulty_source"] = "uoftindex"
    db.update("course", course_id, fields)

    db.insert("ingest_log", {
        "kind": "uoftindex",
        "filename": bundle["code"],
        "summary": (f"{bundle['code']}: difficulty {verdict.get('difficulty')}/5 "
                    f"({verdict.get('confidence')}) [{source}]"),
        "payload": db.jdump({"signals": signals, "verdict": verdict}),
    })
    recompute_priorities()
    return {"ok": True, "course": get_course(course_id), "verdict": verdict,
            "signals": signals, "source": source, "fetched_from": bundle["source"]}


def lookup_all_difficulties(use_cache: bool = True) -> list[dict]:
    return [lookup_difficulty(c["id"], use_cache) for c in list_courses()]


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]


def get_availability() -> list[dict]:
    """The student's declared study window for each weekday, Monday first."""
    rows = db.query("SELECT * FROM availability ORDER BY weekday")
    by_day = {r["weekday"]: r for r in rows}
    out = []
    for i, name in enumerate(WEEKDAY_NAMES):
        r = by_day.get(i, {"weekday": i, "available": 1,
                           "start_time": "17:00", "end_time": "22:00"})
        out.append({**r, "day": name})
    return out


def save_availability(rows: list[dict]) -> None:
    """``rows`` = [{weekday, available, start_time, end_time}, ...]."""
    for r in rows:
        db.execute(
            "INSERT INTO availability (weekday, available, start_time, end_time) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(weekday) DO UPDATE SET "
            "available = excluded.available, start_time = excluded.start_time, "
            "end_time = excluded.end_time",
            [int(r["weekday"]), 1 if r.get("available") else 0,
             r.get("start_time") or "17:00", r.get("end_time") or "22:00"])


def weekly_available_minutes() -> int:
    total = 0
    for row in get_availability():
        if not row["available"]:
            continue
        total += max(0, _minutes(row["end_time"]) - _minutes(row["start_time"]))
    return total


def _minutes(hhmm: str) -> int:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def build_initial_blocks(horizon_days: int = 14, replace: bool = True) -> dict:
    """THE pre-planning call: empty, dated study blocks weighted by difficulty.

    Requires Gemini -- there is no offline planner. A fabricated timetable that
    looks real is worse than an error.
    """
    student = get_student()
    courses = list_courses()
    if not courses:
        return {"ok": False, "error": "Add at least one course first.",
                "blocks": [], "source": "skipped"}

    availability = [
        {"weekday": r["weekday"], "day": r["day"],
         "start_time": r["start_time"], "end_time": r["end_time"]}
        for r in get_availability() if r["available"]
    ]
    if not availability:
        return {"ok": False, "error": "You haven't marked any day as available.",
                "blocks": [], "source": "skipped"}

    state = {
        "today": today_iso(),
        "today_weekday": WEEKDAY_NAMES[date.today().weekday()],
        "horizon_days": horizon_days,
        "study_habits": student.get("study_habits", ""),
        "courses": [
            {
                "code": c["code"],
                "name": c["name"],
                "difficulty": c["difficulty"],
                "difficulty_source": c["difficulty_source"],
                "uoft_drop_rate": c.get("uoft_drop_rate"),
                "uoft_workload": c.get("uoft_workload"),
                "target_weekly_minutes": c.get("weekly_study_min") or 0,
                "difficulty_reasoning": (c.get("uoft_reasoning") or "")[:300],
            }
            for c in courses
        ],
        "availability": availability,
        "preferences": {
            "daily_minutes": student.get("daily_capacity_min", 180),
            "session_minutes": student.get("session_minutes", 90),
            "break_minutes": student.get("break_minutes", 15),
            "preferred_windows": student.get("preferred_windows", []),
        },
    }

    try:
        payload, source = gemini.plan_blocks(state)
    except gemini.GeminiRequired as exc:
        return {"ok": False, "error": str(exc), "blocks": [], "source": "unavailable"}

    if replace:
        db.execute("DELETE FROM session WHERE stage = 'preplan' AND date >= ?",
                   [today_iso()])

    kept = 0
    for b in payload.get("blocks", []):
        course = course_by_code(b.get("course_code", ""))
        db.insert("session", {
            "date": b.get("date") or today_iso(),
            "start_time": b.get("start_time") or "17:00",
            "end_time": b.get("end_time") or "18:30",
            "course_id": course["id"] if course else None,
            "focus": b.get("label") or f"{b.get('course_code', '')} study block",
            "rationale": b.get("rationale") or "",
            "todo_ids": "[]",   # intentionally empty -- a later stage fills these
            "stage": "preplan",
        })
        kept += 1

    return {"ok": True, "payload": payload, "source": source, "planned": kept,
            "availability": availability}


# --------------------------------------------------------------------------
# todos, assessments, priorities
# --------------------------------------------------------------------------

def list_assessments(course_id: int | None = None) -> list[dict]:
    if course_id:
        return db.query(
            "SELECT a.*, c.code AS course_code FROM assessment a JOIN course c ON c.id = a.course_id "
            "WHERE a.course_id = ? ORDER BY a.due_date IS NULL, a.due_date", [course_id])
    return db.query(
        "SELECT a.*, c.code AS course_code FROM assessment a JOIN course c ON c.id = a.course_id "
        "ORDER BY a.due_date IS NULL, a.due_date")


def list_topics(course_id: int | None = None) -> list[dict]:
    sql = ("SELECT t.*, c.code AS course_code FROM topic t JOIN course c ON c.id = t.course_id ")
    if course_id:
        return db.query(sql + "WHERE t.course_id = ? ORDER BY t.week, t.name", [course_id])
    return db.query(sql + "ORDER BY c.code, t.week, t.name")


def list_todos(status: str | None = "open") -> list[dict]:
    sql = ("SELECT td.*, c.code AS course_code, c.colour, t.name AS topic_name "
           "FROM todo td JOIN course c ON c.id = td.course_id "
           "LEFT JOIN topic t ON t.id = td.topic_id ")
    if status:
        return db.query(sql + "WHERE td.status = ? ORDER BY td.priority DESC", [status])
    return db.query(sql + "ORDER BY td.priority DESC")


def mastery_by_topic() -> dict[int, float]:
    rows = db.query("SELECT topic_id, AVG(score) AS s FROM mastery "
                    "WHERE topic_id IS NOT NULL GROUP BY topic_id")
    return {r["topic_id"]: r["s"] for r in rows}


def recompute_priorities() -> list[dict]:
    """Re-score every open TODO. Called after anything that changes the inputs."""
    todos = db.query("SELECT * FROM todo WHERE status = 'open'")
    courses = {c["id"]: c for c in list_courses()}
    assessments = {a["id"]: a for a in list_assessments()}
    ranked = scheduler.rank(todos, courses, assessments, mastery_by_topic())
    for t in ranked:
        db.update("todo", t["id"], {"priority": t["priority"], "priority_why": t["priority_why"]})
    return ranked


def set_todo_status(todo_id: int, status: str) -> None:
    db.update("todo", todo_id, {"status": status})


def add_todo(course_id: int, title: str, est_minutes: int = 45,
             due_date: str | None = None, detail: str = "") -> int:
    tid = db.insert("todo", {"course_id": course_id, "title": title, "detail": detail,
                             "est_minutes": est_minutes, "due_date": due_date or None})
    recompute_priorities()
    return tid


def forecast() -> list[dict]:
    student = get_student()
    return scheduler.workload_forecast(list_todos(), student.get("daily_capacity_min", 180))


# --------------------------------------------------------------------------
# scheduling
# --------------------------------------------------------------------------

def build_schedule(horizon_days: int = 7, busy_blocks: list[dict] | None = None,
                   replace: bool = True) -> dict:
    """Ranked TODOs + constraints -> dated calendar blocks."""
    student = get_student()
    ranked = recompute_priorities()
    courses = {c["id"]: c for c in list_courses()}

    state = {
        "today": today_iso(),
        "horizon_days": horizon_days,
        "study_habits": student.get("study_habits", ""),
        "preferences": {
            "daily_minutes": student.get("daily_capacity_min", 180),
            "session_minutes": student.get("session_minutes", 90),
            "break_minutes": student.get("break_minutes", 15),
            "preferred_windows": student.get("preferred_windows", []),
            "day_start": "09:00",
            "day_end": "22:00",
        },
        "busy_blocks": busy_blocks or [],
        "todos": [
            {
                "title": t["title"],
                "course_code": courses.get(t["course_id"], {}).get("code", ""),
                "est_minutes": t["est_minutes"],
                "due_date": t.get("due_date"),
                "priority": t["priority"],
                "priority_why": t["priority_why"],
            }
            for t in ranked[:25]
        ],
    }

    payload, source = gemini.plan_week(state)

    if replace:
        db.execute("DELETE FROM session WHERE status = 'planned' AND date >= ?", [today_iso()])

    todo_by_title = {t["title"].lower(): t["id"] for t in ranked}
    for s in payload.get("sessions", []):
        course = course_by_code(s.get("course_code", ""))
        ids = [todo_by_title[t.lower()] for t in s.get("todo_titles", [])
               if t.lower() in todo_by_title]
        db.insert("session", {
            "date": s.get("date") or today_iso(),
            "start_time": s.get("start_time") or "09:00",
            "end_time": s.get("end_time") or "10:30",
            "course_id": course["id"] if course else None,
            "focus": s.get("focus") or "",
            "rationale": s.get("rationale") or "",
            "todo_ids": db.jdump(ids),
        })
    return {"payload": payload, "source": source, "planned": len(payload.get("sessions", []))}


def list_sessions(from_date: str | None = None) -> list[dict]:
    rows = db.query(
        "SELECT s.*, c.code AS course_code, c.colour FROM session s "
        "LEFT JOIN course c ON c.id = s.course_id "
        "WHERE s.date >= ? ORDER BY s.date, s.start_time",
        [from_date or today_iso()])
    for r in rows:
        r["todo_ids"] = db.jload(r["todo_ids"], [])
    return rows


def set_session_status(session_id: int, status: str) -> None:
    db.update("session", session_id, {"status": status})
    if status == "done":
        row = db.query_one("SELECT todo_ids FROM session WHERE id = ?", [session_id])
        for tid in db.jload((row or {}).get("todo_ids"), []):
            db.update("todo", tid, {"status": "done"})
        recompute_priorities()


# --------------------------------------------------------------------------
# Part 2 -- questions, grading, mastery
# --------------------------------------------------------------------------

def make_quiz(course_id: int, topic_id: int | None, n: int = 5,
              difficulty: int = 3, attachments: list[Attachment] | None = None) -> dict:
    course = get_course(course_id)
    topic = db.query_one("SELECT * FROM topic WHERE id = ?", [topic_id]) if topic_id else None
    weak = [r["label"] for r in db.query(
        "SELECT label FROM mastery WHERE course_id = ? AND score < 0.6 ORDER BY score LIMIT 6",
        [course_id])]

    payload, source = gemini.generate_questions(
        course["code"],
        (topic or {}).get("name", course["name"] or course["code"]),
        (topic or {}).get("summary", ""),
        n=n, difficulty=difficulty, weak_points=weak, attachments=attachments,
    )

    ids = []
    for q in payload.get("questions", []):
        ids.append(db.insert("question", {
            "course_id": course_id,
            "topic_id": topic_id,
            "prompt": q.get("prompt") or "",
            "answer": q.get("answer") or "",
            "kind": q.get("kind") or "short",
            "options": db.jdump(q.get("options") or []),
            "difficulty": int(q.get("difficulty") or difficulty),
        }))
    return {"question_ids": ids, "source": source, "targeted_weak_points": weak}


def add_past_paper(course_id: int, topic_id: int | None, text: str,
                   attachments: list[Attachment] | None = None) -> dict:
    """A real past/mock test the student uploaded, stored as gradeable questions."""
    course = get_course(course_id)
    topic = db.query_one("SELECT * FROM topic WHERE id = ?", [topic_id]) if topic_id else None
    payload, source = gemini.generate_questions(
        course["code"],
        (topic or {}).get("name", "past paper"),
        f"Transcribe and normalise the questions in this past paper. {text}",
        n=10, difficulty=3, attachments=attachments,
    )
    ids = [db.insert("question", {
        "course_id": course_id, "topic_id": topic_id,
        "prompt": q.get("prompt") or "", "answer": q.get("answer") or "",
        "kind": q.get("kind") or "short", "options": db.jdump(q.get("options") or []),
        "difficulty": int(q.get("difficulty") or 3), "origin": "past_paper",
    }) for q in payload.get("questions", [])]
    return {"question_ids": ids, "source": source}


def get_questions(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = db.query(f"SELECT * FROM question WHERE id IN ({marks})", ids)
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def list_questions(course_id: int | None = None) -> list[dict]:
    sql = ("SELECT q.*, c.code AS course_code, t.name AS topic_name, "
           "(SELECT COUNT(*) FROM attempt a WHERE a.question_id = q.id) AS attempts "
           "FROM question q JOIN course c ON c.id = q.course_id "
           "LEFT JOIN topic t ON t.id = q.topic_id ")
    if course_id:
        return db.query(sql + "WHERE q.course_id = ? ORDER BY q.id DESC", [course_id])
    return db.query(sql + "ORDER BY q.id DESC")


def submit_quiz(answers: dict[int, str]) -> dict:
    """Grade a sitting. Detects retakes and tells Gemini, per the blackboard."""
    questions = get_questions(list(answers))
    if not questions:
        return {"results": [], "overall": "Nothing submitted.", "source": "none"}

    prior = db.query(
        "SELECT question_id, COUNT(*) AS n FROM attempt WHERE question_id IN "
        f"({','.join('?' for _ in questions)}) GROUP BY question_id",
        [q["id"] for q in questions])
    prior_counts = {r["question_id"]: r["n"] for r in prior}
    is_retake = any(prior_counts.get(q["id"], 0) > 0 for q in questions)

    student = get_student()
    items = [{
        "question_id": q["id"],
        "prompt": q["prompt"],
        "answer": q["answer"],
        "student_answer": answers.get(q["id"], ""),
    } for q in questions]

    payload, source = gemini.grade_attempts(
        items, is_retake=is_retake, study_habits=student.get("study_habits", ""))

    q_by_id = {q["id"]: q for q in questions}
    for r in payload.get("results", []):
        qid = int(r.get("question_id") or 0)
        q = q_by_id.get(qid)
        if not q:
            continue
        score = max(0.0, min(1.0, float(r.get("score") or 0)))
        db.insert("attempt", {
            "question_id": qid,
            "answer": answers.get(qid, ""),
            "score": score,
            "feedback": r.get("feedback") or "",
            "gaps": db.jdump(r.get("gaps") or []),
            "attempt_no": prior_counts.get(qid, 0) + 1,
        })
        if q.get("topic_id"):
            topic = db.query_one("SELECT name FROM topic WHERE id = ?", [q["topic_id"]])
            _bump_mastery(q["course_id"], (topic or {}).get("name", "topic"),
                          score, topic_id=q["topic_id"])
        for gap in (r.get("gaps") or []):
            if str(gap).strip().lower() in NON_GAPS:
                continue  # bookkeeping tags from the offline grader, not real misconceptions
            _bump_mastery(q["course_id"], str(gap)[:60], score, topic_id=q.get("topic_id"))

    recompute_priorities()
    return {**payload, "source": source, "is_retake": is_retake}


def _bump_mastery(course_id: int, label: str, score: float, topic_id: int | None) -> None:
    """Exponential moving average so recent evidence dominates but history counts."""
    label = (label or "").strip()[:60]
    if not label:
        return
    row = db.query_one(
        "SELECT * FROM mastery WHERE course_id = ? AND label = ? COLLATE NOCASE",
        [course_id, label])
    if row:
        alpha = 0.4
        new = (1 - alpha) * row["score"] + alpha * score
        db.update("mastery", row["id"], {
            "score": round(new, 3),
            "samples": row["samples"] + 1,
            "topic_id": topic_id or row["topic_id"],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
    else:
        db.insert("mastery", {"course_id": course_id, "topic_id": topic_id,
                              "label": label, "score": round(score, 3), "samples": 1})


def list_mastery() -> list[dict]:
    return db.query(
        "SELECT m.*, c.code AS course_code FROM mastery m JOIN course c ON c.id = m.course_id "
        "ORDER BY m.score ASC")


def list_attempts(limit: int = 50) -> list[dict]:
    rows = db.query(
        "SELECT a.*, q.prompt, q.difficulty, c.code AS course_code, t.name AS topic_name "
        "FROM attempt a JOIN question q ON q.id = a.question_id "
        "JOIN course c ON c.id = q.course_id LEFT JOIN topic t ON t.id = q.topic_id "
        "ORDER BY a.id DESC LIMIT ?", [limit])
    for r in rows:
        r["gaps"] = db.jload(r["gaps"], [])
    return rows


# --------------------------------------------------------------------------
# Part 2 -- coaching
# --------------------------------------------------------------------------

def run_diagnosis() -> dict:
    """What is this student actually struggling with?"""
    snapshot = {
        "mastery": list_mastery(),
        "recent_attempts": [
            {"course_code": a["course_code"], "topic": a.get("topic_name"),
             "prompt": a["prompt"][:160], "score": a["score"],
             "gaps": a["gaps"], "attempt_no": a["attempt_no"]}
            for a in list_attempts(30)
        ],
        "courses": [{"code": c["code"], "name": c["name"], "difficulty": c["difficulty"]}
                    for c in list_courses()],
        "study_habits": get_student().get("study_habits", ""),
    }
    payload, source = gemini.diagnose(snapshot)
    return {**payload, "source": source}


def run_resources(struggles: list[dict] | None = None) -> dict:
    if struggles is None:
        struggles = [
            {"label": m["label"], "course_code": m["course_code"],
             "severity": "high" if m["score"] < 0.4 else "medium",
             "evidence": f"mastery {m['score']:.0%}"}
            for m in list_mastery() if m["score"] < 0.7
        ][:8]
    if not struggles:
        return {"resources": [], "source": "skipped", "note": "No weak points recorded yet."}

    courses = [{"code": c["code"], "name": c["name"], "textbook": c["textbook"]}
               for c in list_courses()]
    payload, source = gemini.recommend_resources(
        struggles, courses, get_student().get("study_habits", ""))

    db.execute("DELETE FROM resource")
    for r in payload.get("resources", []):
        course = course_by_code(r.get("course_code", ""))
        if not course:
            continue
        db.insert("resource", {
            "course_id": course["id"], "label": r.get("label") or "",
            "title": r.get("title") or "", "kind": r.get("kind") or "reading",
            "locator": r.get("locator") or "", "why": r.get("why") or "",
        })
    return {**payload, "source": source}


def list_resources() -> list[dict]:
    return db.query(
        "SELECT r.*, c.code AS course_code FROM resource r JOIN course c ON c.id = r.course_id "
        "ORDER BY c.code, r.label")


def run_overlaps() -> dict:
    courses = [{"code": c["code"], "name": c["name"]} for c in list_courses()]
    topics = [{"course_code": t["course_code"], "name": t["name"], "summary": t["summary"][:200]}
              for t in list_topics()]
    if len(courses) < 2:
        return {"overlaps": [], "source": "skipped",
                "note": "Add at least two courses to find overlap."}

    payload, source = gemini.find_overlaps(courses, topics)
    db.execute("DELETE FROM overlap")
    for o in payload.get("overlaps", []):
        db.insert("overlap", {
            "label": o.get("label") or "",
            "course_ids": db.jdump(o.get("course_codes") or []),
            "payoff": o.get("payoff") or "",
            "saved_min": int(o.get("saved_minutes") or 0),
        })
    return {**payload, "source": source}


def list_overlaps() -> list[dict]:
    rows = db.query("SELECT * FROM overlap ORDER BY saved_min DESC")
    for r in rows:
        r["course_ids"] = db.jload(r["course_ids"], [])
    return rows


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------

def ingest_log(limit: int = 25) -> list[dict]:
    return db.query("SELECT * FROM ingest_log ORDER BY id DESC LIMIT ?", [limit])


def stats() -> dict:
    def one(sql: str) -> int:
        return (db.query_one(sql) or {}).get("n", 0)

    return {
        "courses": one("SELECT COUNT(*) n FROM course"),
        "assessments": one("SELECT COUNT(*) n FROM assessment"),
        "topics": one("SELECT COUNT(*) n FROM topic"),
        "open_todos": one("SELECT COUNT(*) n FROM todo WHERE status='open'"),
        "done_todos": one("SELECT COUNT(*) n FROM todo WHERE status='done'"),
        "sessions": one("SELECT COUNT(*) n FROM session WHERE status='planned'"),
        "questions": one("SELECT COUNT(*) n FROM question"),
        "attempts": one("SELECT COUNT(*) n FROM attempt"),
    }


def export_state() -> str:
    return json.dumps({
        "student": get_student(),
        "courses": list_courses(),
        "assessments": list_assessments(),
        "topics": list_topics(),
        "todos": list_todos(None),
        "sessions": list_sessions("1900-01-01"),
        "mastery": list_mastery(),
        "resources": list_resources(),
        "overlaps": list_overlaps(),
    }, indent=2, default=str)


def reset() -> None:
    db.reset_db()
