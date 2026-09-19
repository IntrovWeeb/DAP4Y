"""One-click demo data.

Deliberately runs the *real* ingestion pipeline over the sample files rather
than inserting rows directly -- so the seeded state is exactly what a judge
would get by uploading those files themselves.
"""

from __future__ import annotations

from pathlib import Path

from . import app, db

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"

HABITS = (
    "I cram the night before and it stops working past week 6. I learn by doing "
    "problems, not by re-reading notes or watching long videos. Sharpest 20:00-23:00, "
    "useless before 10:00. I avoid the courses I'm worst at, which is the real problem."
)


def load_samples() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(SAMPLES.glob("*.txt"))}


def seed(reset: bool = True) -> dict:
    if reset:
        app.reset()
    else:
        app.bootstrap()

    app.save_student(
        name="Demo Student",
        term="Fall 2026",
        study_habits=HABITS,
        preferred_windows=["weekday evenings 19:00-23:00", "Sunday afternoons"],
        session_minutes=90,
        break_minutes=15,
        daily_capacity_min=180,
    )

    files = load_samples()
    report: dict = {"syllabi": [], "notes": [], "intent": None}

    for name in ("csc373h1_syllabus.txt", "sta257h1_syllabus.txt",
                 "mat137y1_syllabus.txt"):
        if name in files:
            res = app.ingest_syllabus(files[name], filename=name)
            report["syllabi"].append(
                {"file": name, "course": res["course"]["code"],
                 "created": res["created"], "source": res["source"]})

    for name, code in (("csc373h1_week6_notes.txt", "CSC373H1"),
                       ("sta257h1_week7_notes.txt", "STA257H1")):
        course = app.course_by_code(code)
        if name in files and course:
            res = app.ingest_notes(course["id"], files[name], filename=name)
            report["notes"].append({"file": name, "course": code, "source": res["source"]})

    report["intent"] = app.ingest_intent(
        "I work shifts Monday and Wednesday 9 to 5, I have a lab every Tuesday "
        "afternoon, and I want to study about 3 hours a day in the evenings."
    )

    _seed_history()
    app.recompute_priorities()
    report["stats"] = app.stats()
    return report


def _seed_history() -> None:
    """A little attempt history so the coaching tabs have something to chew on."""
    history = [
        ("CSC373H1", "Shortest paths", "why Dijkstra fails on negative edges", 0.35,
         ["assumes greedy choice is final", "ignores negative edges"]),
        ("CSC373H1", "Shortest paths", "trace Bellman-Ford and detect a negative cycle", 0.5,
         ["confuses relaxation rounds with path length"]),
        ("CSC373H1", "Dynamic programming", "state the optimal substructure property", 0.8, []),
        ("STA257H1", "Central Limit Theorem", "when to use Chebyshev vs the CLT", 0.3,
         ["picks asymptotic tool for a finite-n bound"]),
        ("STA257H1", "Central Limit Theorem", "apply the continuity correction", 0.45,
         ["omits continuity correction"]),
        ("STA257H1", "Law of Large Numbers", "distinguish convergence in probability vs distribution",
         0.4, ["conflates modes of convergence"]),
    ]
    for code, topic_name, prompt, score, gaps in history:
        course = app.course_by_code(code)
        if not course:
            continue
        topic = db.query_one(
            "SELECT id FROM topic WHERE course_id = ? AND name LIKE ? LIMIT 1",
            [course["id"], f"%{topic_name.split()[0]}%"])
        qid = db.insert("question", {
            "course_id": course["id"],
            "topic_id": (topic or {}).get("id"),
            "prompt": prompt.capitalize() + ".",
            "answer": "(seeded demo history)",
            "difficulty": 3,
        })
        db.insert("attempt", {
            "question_id": qid, "answer": "(seeded demo history)", "score": score,
            "feedback": "Seeded so the coaching views have history on first run.",
            "gaps": db.jdump(gaps), "attempt_no": 1,
        })
        if topic:
            app._bump_mastery(course["id"], topic_name, score, topic_id=topic["id"])
        for g in gaps:
            app._bump_mastery(course["id"], g, score, topic_id=(topic or {}).get("id"))
