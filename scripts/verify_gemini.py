"""Exercise every Gemini call and report which ones actually hit the API.

Run this the moment you add GEMINI_API_KEY -- it is the fastest way to find out
whether the live path works, without clicking through the whole UI.

    python scripts/verify_gemini.py

Any line marked MOCK means that call silently fell back to the offline parser;
the reason is printed beside it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# keep this out of the real database
os.environ.setdefault("DAP4Y_DB", str(REPO / "data" / "verify.db"))

from src.backend import app as api, gemini, uoftindex  # noqa: E402

GREEN, RED, DIM, OFF = "\033[92m", "\033[91m", "\033[2m", "\033[0m"
results: list[tuple[str, str]] = []


def check(label: str, source: str, detail: str = "") -> None:
    live = source == "gemini"
    tag = f"{GREEN}GEMINI{OFF}" if live else f"{RED}MOCK  {OFF}"
    print(f"  {tag}  {label:22} {DIM}{detail or source}{OFF}")
    results.append((label, source))


def main() -> int:
    print(f"\n{gemini.status()}\n")
    if not gemini.live():
        print(f"{RED}No API key -- every call below will fall back.{OFF}")
        print("Set GEMINI_API_KEY in .env, then re-run.\n")

    api.reset()
    samples = {p.name: p.read_text(encoding="utf-8")
               for p in (REPO / "data" / "samples").glob("*.txt")}

    print("PART 1 -- ingestion")
    syl = samples.get("csc373h1_syllabus.txt", "")
    r = api.ingest_syllabus(syl, filename="csc373h1_syllabus.txt")
    check("parse_syllabus", r["source"],
          f"{r['course']['code']} · {r['created']['assessments']} assessments")

    course = api.course_by_code("CSC373H1")
    if course:
        n = api.ingest_notes(course["id"], samples.get("csc373h1_week6_notes.txt", ""))
        check("parse_notes", n["source"],
              f"{len(n['created'].get('todos', []))} todos, "
              f"{len(n['created'].get('flagged_confusions', []))} confusions")

    i = api.ingest_intent("I work Monday and Wednesday 9-5 and have a midterm next Tuesday.")
    check("parse_intent", i["source"], i["payload"].get("echo", "")[:50])

    print("\nPRE-PLANNING")
    try:
        bundle = uoftindex.fetch("CSC373H1")
        sig = uoftindex.difficulty_signals(bundle)
        print(f"  {GREEN}SITE  {OFF}  {'uoftindex.ca':22} {DIM}drop {sig['drop_rate_pct']}%, "
              f"workload {sig['workload_5']}, {sig['review_count']} reviews "
              f"({bundle['source']}){OFF}")
    except uoftindex.LookupError_ as exc:
        print(f"  {RED}SITE  {OFF}  {'uoftindex.ca':22} {DIM}{exc}{OFF}")

    if course:
        d = api.lookup_difficulty(course["id"])
        if d["ok"]:
            v = d["verdict"]
            check("assess_difficulty", d["source"],
                  f"{v['difficulty']}/5 ({v['confidence']}) via {v.get('signals_used')}")
        else:
            print(f"  {RED}FAIL  {OFF}  assess_difficulty     {DIM}{d['error']}{OFF}")

    b = api.build_initial_blocks(horizon_days=7)
    if b.get("ok"):
        empty = all(not s["todo_ids"] for s in api.list_sessions()
                    if s.get("stage") == "preplan")
        check("plan_blocks", b["source"],
              f"{b['planned']} blocks, all empty of todos: {empty}")
    else:
        # plan_blocks has no fallback by design, so a failure must be reported
        # rather than silently skipped.
        check("plan_blocks", "FAILED", b.get("error", "")[:70])

    print("\nPART 2 -- study loop")
    if course:
        topics = api.list_topics(course["id"])
        q = api.make_quiz(course["id"], topics[0]["id"] if topics else None, n=3)
        check("generate_questions", q["source"], f"{len(q['question_ids'])} questions")

        qs = api.get_questions(q["question_ids"])
        if qs:
            g = api.submit_quiz({qs[0]["id"]: "Dijkstra is greedy; it needs "
                                              "non-negative weights because a popped "
                                              "node's distance must be final."})
            check("grade_attempts", g["source"], g.get("overall", "")[:50])

    d = api.run_diagnosis()
    check("diagnose", d["source"], f"{len(d.get('struggles', []))} struggles")
    rr = api.run_resources()
    check("recommend_resources", rr["source"], f"{len(rr.get('resources', []))} resources")
    ov = api.run_overlaps()
    check("find_overlaps", ov["source"], f"{len(ov.get('overlaps', []))} overlaps")

    live = sum(1 for _, s in results if s == "gemini")
    print(f"\n{'=' * 62}")
    print(f"  {live}/{len(results)} calls hit Gemini")
    for label, source in results:
        if source != "gemini":
            print(f"  {RED}!{OFF} {label}: {source}")
    print(f"{'=' * 62}\n")

    db_path = Path(os.environ["DAP4Y_DB"])
    if db_path.exists() and db_path.name == "verify.db":
        db_path.unlink()
    return 0 if live == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
