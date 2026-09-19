"""Priority scoring and schedule assembly.

Deliberately NOT a Gemini call. Gemini extracts the facts; this module ranks
them with arithmetic the student can audit and argue with. That separation is
the point of the project: the LLM is the I/O layer, not the decision-maker.

    priority = 100 x (W_weight  x grade_weight
                    + W_urgency x urgency
                    + W_difficulty x course_difficulty
                    + W_weakness x weakness)
"""

from __future__ import annotations

from datetime import date, timedelta

WEIGHTS = {
    "grade": 0.40,       # how much of the final grade rides on it
    "urgency": 0.30,     # how soon it is due
    "difficulty": 0.15,  # how hard the course is for this student
    "weakness": 0.15,    # how badly they're doing on the topic
}


def _parse(d: str | None) -> date | None:
    try:
        return date.fromisoformat(d) if d else None
    except (ValueError, TypeError):
        return None


def urgency(due: str | None, today: date, horizon: int = 21) -> float:
    """1.0 = due today or overdue, decaying to 0 at the horizon."""
    d = _parse(due)
    if d is None:
        return 0.25  # undated work still deserves some pull
    days = (d - today).days
    if days <= 0:
        return 1.0
    return max(0.0, 1.0 - days / horizon)


def score_todo(todo: dict, course: dict, assessment: dict | None,
               mastery: float | None, today: date) -> tuple[float, str]:
    """Return (0-100 priority, human-readable explanation)."""
    grade_w = float(assessment["weight_pct"]) / 100 if assessment else 0.10
    grade_w = min(grade_w, 1.0)

    due = (todo.get("due_date") or (assessment or {}).get("due_date"))
    urg = urgency(due, today)

    diff = (float(course.get("difficulty", 3)) - 1) / 4  # 1..5 -> 0..1
    weak = 1.0 - (mastery if mastery is not None else 0.5)

    total = 100 * (
        WEIGHTS["grade"] * grade_w
        + WEIGHTS["urgency"] * urg
        + WEIGHTS["difficulty"] * diff
        + WEIGHTS["weakness"] * weak
    )

    bits = []
    if assessment:
        bits.append(f"{assessment['weight_pct']:g}% of grade ({assessment['title']})")
    else:
        bits.append("not tied to a graded item")
    d = _parse(due)
    if d:
        days = (d - today).days
        bits.append("overdue" if days < 0 else "due today" if days == 0 else f"due in {days}d")
    else:
        bits.append("no due date")
    bits.append(f"course difficulty {course.get('difficulty', 3)}/5")
    if mastery is not None:
        bits.append(f"mastery {mastery:.0%}")
    return round(total, 1), "; ".join(bits)


def rank(todos: list[dict], courses: dict[int, dict],
         assessments: dict[int, dict], mastery_by_topic: dict[int, float],
         today: date | None = None) -> list[dict]:
    """Score every open TODO and return it sorted, highest priority first."""
    today = today or date.today()
    out = []
    for t in todos:
        course = courses.get(t["course_id"], {"difficulty": 3})
        assessment = assessments.get(t.get("assessment_id")) if t.get("assessment_id") else None
        m = mastery_by_topic.get(t.get("topic_id")) if t.get("topic_id") else None
        score, why = score_todo(t, course, assessment, m, today)
        out.append({**t, "priority": score, "priority_why": why})
    out.sort(key=lambda r: r["priority"], reverse=True)
    return out


def workload_forecast(todos: list[dict], daily_minutes: int,
                      today: date | None = None, horizon: int = 14) -> list[dict]:
    """Minutes of work owed per day vs. capacity -- surfaces impossible weeks."""
    today = today or date.today()
    demand: dict[str, int] = {}
    for t in todos:
        d = _parse(t.get("due_date")) or (today + timedelta(days=horizon))
        # spread each TODO evenly over the days remaining before it is due
        span = max(1, min((d - today).days, horizon))
        per_day = t.get("est_minutes", 45) / span
        for i in range(span):
            key = (today + timedelta(days=i)).isoformat()
            demand[key] = demand.get(key, 0) + per_day

    return [
        {
            "date": (today + timedelta(days=i)).isoformat(),
            "required_minutes": round(demand.get((today + timedelta(days=i)).isoformat(), 0)),
            "capacity_minutes": daily_minutes,
            "overloaded": demand.get((today + timedelta(days=i)).isoformat(), 0) > daily_minutes,
        }
        for i in range(horizon)
    ]
