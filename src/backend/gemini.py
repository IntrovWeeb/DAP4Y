"""Gemini as the I/O layer.

Every function here takes messy human input (a syllabus PDF, a photo of a
whiteboard, rambling lecture notes, a sentence like "I have an exam Thursday")
and returns a dict that matches a strict JSON schema -- i.e. something the rest
of the application can execute. Gemini is never the product; it is the parser.

If ``GEMINI_API_KEY`` is missing the module falls back to ``mock.py`` so the
prototype is fully demo-able offline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

from . import mock

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

_client = None
_client_error: str | None = None


def _redact(exc: Exception) -> str:
    """Exception text is shown in the UI, so scrub anything key-shaped first.

    The Gemini REST API passes the key as a ``?key=`` query parameter, so a
    transport-level error can quote it back at us inside a URL.
    """
    text = f"{type(exc).__name__}: {exc}"
    if _API_KEY:
        text = text.replace(_API_KEY, "<redacted>")
    text = re.sub(r"(?i)\b(key|api[_-]?key|token)=[^\s&'\"]+", r"\1=<redacted>", text)
    text = re.sub(r"\bAIza[0-9A-Za-z_\-]{10,}", "<redacted>", text)
    return text[:200]


def live() -> bool:
    """True when a real Gemini call is possible."""
    return _get_client() is not None


def status() -> str:
    if live():
        return f"Gemini live ({MODEL})"
    return f"Mock mode -- {_client_error or 'no GEMINI_API_KEY set'}"


def _get_client():
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client
    if not _API_KEY:
        _client_error = "no GEMINI_API_KEY set"
        return None
    try:
        from google import genai  # noqa: PLC0415

        _client = genai.Client(api_key=_API_KEY)
    except Exception as exc:  # pragma: no cover - env dependent
        _client_error = _redact(exc)
        _client = None
    return _client


@dataclass
class Attachment:
    """A file the student dropped on us: PDF, image, audio, whatever."""

    filename: str
    mime_type: str
    data: bytes


# --------------------------------------------------------------------------
# low level call
# --------------------------------------------------------------------------

def _call(
    *,
    system: str,
    prompt: str,
    schema: dict,
    attachments: list[Attachment] | None = None,
    temperature: float = 0.2,
) -> dict:
    client = _get_client()
    if client is None:
        raise RuntimeError(_client_error or "Gemini unavailable")

    from google.genai import types  # noqa: PLC0415

    parts: list[Any] = [types.Part.from_text(text=prompt)]
    for att in attachments or []:
        parts.append(types.Part.from_bytes(data=att.data, mime_type=att.mime_type))

    resp = client.models.generate_content(
        model=MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return json.loads(text)


def _safe(fn, fallback, *args, **kwargs) -> tuple[dict, str]:
    """Run a Gemini call, degrading to the mock on any failure.

    Returns ``(payload, source)`` where source is 'gemini' or 'mock: <reason>'.
    """
    if not live():
        return fallback(*args, **kwargs), f"mock ({_client_error})"
    try:
        return fn(*args, **kwargs), "gemini"
    except Exception as exc:
        return fallback(*args, **kwargs), f"mock (call failed: {_redact(exc)})"


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

STR = {"type": "string"}
NUM = {"type": "number"}
INT = {"type": "integer"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required or list(props),
    }


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


SYLLABUS_SCHEMA = _obj(
    {
        "course": _obj(
            {
                "code": STR,
                "name": STR,
                "instructor": STR,
                "textbook": STR,
                "credits": NUM,
                "difficulty": INT,
                "difficulty_reason": STR,
            }
        ),
        "assessments": _arr(
            _obj(
                {
                    "title": STR,
                    "kind": {
                        "type": "string",
                        "enum": ["assignment", "quiz", "midterm", "final", "project", "lab"],
                    },
                    "due_date": STR,
                    "weight_pct": NUM,
                    "notes": STR,
                }
            )
        ),
        "topics": _arr(_obj({"name": STR, "week": INT, "summary": STR})),
        "warnings": _arr(STR),
    }
)

NOTES_SCHEMA = _obj(
    {
        "course_code": STR,
        "topics": _arr(
            _obj(
                {
                    "name": STR,
                    "source": {"type": "string", "enum": ["lecture", "tutorial", "lab", "reading"]},
                    "week": INT,
                    "summary": STR,
                    "keywords": _arr(STR),
                }
            )
        ),
        "todos": _arr(
            _obj(
                {
                    "title": STR,
                    "detail": STR,
                    "topic": STR,
                    "est_minutes": INT,
                    "kind": {
                        "type": "string",
                        "enum": ["review", "practice", "reading", "admin", "clarify"],
                    },
                }
            )
        ),
        "flagged_confusions": _arr(STR),
    }
)

QUESTIONS_SCHEMA = _obj(
    {
        "questions": _arr(
            _obj(
                {
                    "prompt": STR,
                    "answer": STR,
                    "kind": {"type": "string", "enum": ["short", "mcq", "numeric", "proof"]},
                    "options": _arr(STR),
                    "difficulty": INT,
                    "targets": STR,
                }
            )
        )
    }
)

GRADE_SCHEMA = _obj(
    {
        "results": _arr(
            _obj(
                {
                    "question_id": INT,
                    "score": NUM,
                    "verdict": {
                        "type": "string",
                        "enum": ["correct", "partial", "incorrect", "blank"],
                    },
                    "feedback": STR,
                    "gaps": _arr(STR),
                }
            )
        ),
        "overall": STR,
    }
)

DIAGNOSIS_SCHEMA = _obj(
    {
        "struggles": _arr(
            _obj(
                {
                    "label": STR,
                    "course_code": STR,
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "evidence": STR,
                    "fix": STR,
                }
            )
        ),
        "strengths": _arr(STR),
        "coach_note": STR,
    }
)

RESOURCES_SCHEMA = _obj(
    {
        "resources": _arr(
            _obj(
                {
                    "label": STR,
                    "course_code": STR,
                    "title": STR,
                    "kind": {"type": "string", "enum": ["reading", "video", "practice", "tool"]},
                    "locator": STR,
                    "why": STR,
                }
            )
        )
    }
)

OVERLAP_SCHEMA = _obj(
    {
        "overlaps": _arr(
            _obj(
                {
                    "label": STR,
                    "course_codes": _arr(STR),
                    "payoff": STR,
                    "saved_minutes": INT,
                }
            )
        )
    }
)

PLAN_SCHEMA = _obj(
    {
        "sessions": _arr(
            _obj(
                {
                    "date": STR,
                    "start_time": STR,
                    "end_time": STR,
                    "course_code": STR,
                    "focus": STR,
                    "rationale": STR,
                    "todo_titles": _arr(STR),
                }
            )
        ),
        "constraints_understood": _arr(STR),
        "tradeoffs": _arr(STR),
    }
)

INTENT_SCHEMA = _obj(
    {
        "busy_blocks": _arr(
            _obj({"date": STR, "start_time": STR, "end_time": STR, "label": STR})
        ),
        "deadlines": _arr(
            _obj({"course_code": STR, "title": STR, "date": STR, "kind": STR})
        ),
        "preferences": _obj(
            {
                "daily_minutes": INT,
                "preferred_windows": _arr(STR),
                "session_minutes": INT,
                "notes": STR,
            }
        ),
        "echo": STR,
    }
)


# --------------------------------------------------------------------------
# Part 1 -- messy input  ->  structured semester
# --------------------------------------------------------------------------

_SYSTEM = (
    "You are the parsing layer of DAP4Y, a semester planner for university students. "
    "You never chat. You read messy human material and emit JSON matching the given "
    "schema exactly. Dates are ISO yyyy-mm-dd; if a date is relative or missing, infer "
    "it from the stated term and say so in a warning/notes field. Never invent "
    "weightages that are not present -- use 0 and note it instead."
)


def parse_syllabus(text: str, attachments: list[Attachment] | None = None,
                   today: str = "", term: str = "") -> tuple[dict, str]:
    """Syllabus / portal screenshots -> course, deadlines, textbook, weightages."""

    def _run():
        prompt = (
            f"Today is {today}. Term: {term or 'unspecified'}.\n"
            "Extract the course, every graded assessment with its due date and weight "
            "(as a percentage of the final grade), the textbook, and the topic outline.\n"
            "Also estimate course difficulty 1-5 for a typical student, using workload, "
            "assessment density, prerequisites and topic abstraction. Explain in "
            "difficulty_reason. The student may override this later.\n\n"
            f"MATERIAL:\n{text or '(see attached files)'}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=SYLLABUS_SCHEMA,
                     attachments=attachments)

    return _safe(_run, mock.parse_syllabus, text, today=today)


def parse_notes(text: str, course_code: str, attachments: list[Attachment] | None = None,
                today: str = "", understanding_context: str = "",
                energy_level: int | None = None) -> tuple[dict, str]:
    """Lecture/tutorial notes -> topics + concrete TODOs for a study session."""

    def _run():
        prompt = (
            f"Today is {today}. These are notes for course {course_code}.\n"
            f"Student self-reported understanding: {understanding_context or 'No extra context provided.'}\n"
            f"Student energy level today: {energy_level if energy_level is not None else 'not provided'}/10\n"
            "Use this to calibrate task intensity and confidence. Lower energy should "
            "shift the recommended TODOs toward shorter, more focused tasks, while "
            "confusion should raise the priority of targeted repair work.\n"
            "Pull out the topics covered, and turn the material into concrete study "
            "TODOs -- each one small enough to finish in a single sitting, with an "
            "honest est_minutes. Prefer active verbs ('derive', 'redo Q4', 'rewrite the "
            "proof from memory') over 'review chapter 3'. If the notes contain a "
            "question mark, a '??', a 'ask prof', or visibly trail off, add that to "
            "flagged_confusions -- that is where the student is struggling. Additionally, make sure"
            "to consider the student's english proficiency, their study habits, and what they"
            "already understand.\n\n"
            f"NOTES:\n{text or '(see attached files)'}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=NOTES_SCHEMA,
                     attachments=attachments)

    return _safe(_run, mock.parse_notes, text, course_code, understanding_context, energy_level)


def parse_intent(text: str, today: str = "") -> tuple[dict, str]:
    """The 'Life Compiler' front door: one sentence -> structured constraints."""

    def _run():
        prompt = (
            f"Today is {today}.\n"
            "The student described their week in plain language. Convert it into "
            "structured constraints: busy blocks they cannot study in, deadlines they "
            "mentioned, and scheduling preferences. Resolve relative dates ('next "
            "Thursday') against today. Echo back what you understood in one sentence.\n\n"
            f"STUDENT SAID:\n{text}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=INTENT_SCHEMA, temperature=0.1)

    return _safe(_run, mock.parse_intent, text, today=today)


def generate_questions(course_code: str, topic_name: str, topic_summary: str,
                       n: int = 5, difficulty: int = 3,
                       weak_points: list[str] | None = None,
                       attachments: list[Attachment] | None = None) -> tuple[dict, str]:
    """Study questions generated from the actual lecture/tutorial content."""

    def _run():
        weak = ", ".join(weak_points or []) or "none recorded yet"
        prompt = (
            f"Course {course_code}, topic '{topic_name}'.\n"
            f"Topic summary: {topic_summary}\n"
            f"Known weak points for this student: {weak}\n\n"
            f"Write {n} exam-style questions at difficulty {difficulty}/5, grounded in "
            "the topic above and nothing else. Bias roughly half of them toward the "
            "weak points. Give a complete model answer for each and say in `targets` "
            "which skill it probes. Only fill `options` for mcq."
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=QUESTIONS_SCHEMA,
                     attachments=attachments, temperature=0.7)

    return _safe(_run, mock.generate_questions, course_code, topic_name, n)


# --------------------------------------------------------------------------
# Part 2 -- grading, diagnosis, resources, optimisation
# --------------------------------------------------------------------------

def grade_attempts(items: list[dict], is_retake: bool = False,
                   study_habits: str = "") -> tuple[dict, str]:
    """Grade a mock test. ``items`` = [{question_id, prompt, answer, student_answer}]."""

    def _run():
        mode = (
            "This is a RETAKE. The student has seen these questions before, so credit "
            "genuine understanding, not recall of your previous feedback, and say "
            "explicitly whether the gap from last time has closed."
            if is_retake
            else "This is the student's first attempt at this set."
        )
        prompt = (
            f"{mode}\n"
            f"Student's self-described study habits: {study_habits or 'not given'}\n\n"
            "Grade each answer 0.0-1.0. Be a fair but exacting TA: award partial credit "
            "for correct method with an arithmetic slip, and none for a right answer "
            "with no reasoning where reasoning was asked for. In `gaps`, name the "
            "underlying misconception in 2-4 words (e.g. 'confuses big-O with big-Theta') "
            "-- these become the student's weak-point tags, so be consistent and reusable.\n\n"
            f"SUBMISSION:\n{json.dumps(items, ensure_ascii=False, indent=2)}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=GRADE_SCHEMA, temperature=0.1)

    return _safe(_run, mock.grade_attempts, items, is_retake)


def diagnose(snapshot: dict) -> tuple[dict, str]:
    """Look across every attempt + flagged confusion and name what is going wrong."""

    def _run():
        prompt = (
            "Here is everything DAP4Y knows about this student's performance. Identify "
            "what they are actually struggling with -- patterns across courses count "
            "double. Ground every struggle in specific evidence from the data, and give "
            "a concrete next action in `fix`. Also name genuine strengths.\n\n"
            f"SNAPSHOT:\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=DIAGNOSIS_SCHEMA, temperature=0.3)

    return _safe(_run, mock.diagnose, snapshot)


def recommend_resources(struggles: list[dict], courses: list[dict],
                        study_habits: str = "") -> tuple[dict, str]:
    """Resources matched to the weak point AND to how this student actually studies."""

    def _run():
        prompt = (
            f"Study habits: {study_habits or 'not given'}\n"
            "Recommend 1-2 resources per struggle. Match the format to the habits: a "
            "student who says they learn by doing gets practice sets, not 40-minute "
            "videos. Prefer the course's own textbook chapters when they fit -- give the "
            "chapter/section in `locator`. For anything external, put a precise search "
            "query in `locator` rather than a guessed URL, and say why it fits in `why`.\n\n"
            f"COURSES:\n{json.dumps(courses, ensure_ascii=False)}\n\n"
            f"STRUGGLES:\n{json.dumps(struggles, ensure_ascii=False)}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=RESOURCES_SCHEMA, temperature=0.4)

    return _safe(_run, mock.recommend_resources, struggles, courses)


def find_overlaps(courses: list[dict], topics: list[dict]) -> tuple[dict, str]:
    """Where two courses teach the same thing -- study it once, count it twice."""

    def _run():
        prompt = (
            "Find genuine conceptual overlap between these courses' topics -- the same "
            "idea under different names, a shared prerequisite, or a technique reused in "
            "another context. Skip superficial keyword matches. For each, say in "
            "`payoff` how to study it once for both, and estimate saved_minutes "
            "conservatively.\n\n"
            f"COURSES:\n{json.dumps(courses, ensure_ascii=False)}\n\n"
            f"TOPICS:\n{json.dumps(topics, ensure_ascii=False)}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=OVERLAP_SCHEMA, temperature=0.4)

    return _safe(_run, mock.find_overlaps, courses, topics)


def plan_week(state: dict) -> tuple[dict, str]:
    """Ranked TODOs + real-world constraints -> dated, timed calendar blocks."""

    def _run():
        prompt = (
            "Build a study schedule. The TODOs are already ranked by DAP4Y's priority "
            "score (grade weight x urgency x course difficulty x weakness) -- respect "
            "that order unless a hard deadline forces otherwise, and say so in "
            "`tradeoffs` when you deviate.\n"
            "Rules: never schedule inside a busy block; stay within daily_minutes; keep "
            "each block near session_minutes; prefer the student's preferred windows; "
            "avoid scheduling a block that overlaps with any adversary events (like accidents or appointments);"
            "put the hardest material in their stated peak hours; interleave courses "
            "rather than blocking one course all day; leave the day before a deadline "
            "for that deadline's work; consider the student's past tests and"
            "(especially) upcoming tests/exams when building. Every block needs a one-line `rationale` the "
            "student will find convincing.\n\n"
            f"STATE:\n{json.dumps(state, ensure_ascii=False, indent=2)}"
        )
        return _call(system=_SYSTEM, prompt=prompt, schema=PLAN_SCHEMA, temperature=0.3)

    return _safe(_run, mock.plan_week, state)
