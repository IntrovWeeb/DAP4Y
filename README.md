# DAP4Y — Dynamic Assistant Professor 4 You

A semester manager for students where **Gemini is the I/O layer, not the product**.

You hand it your mess — syllabi, portal screenshots, lecture notes, a photo of a
whiteboard, or one sentence about your week. Gemini parses all of it into strict
JSON. A plain-Python priority engine ranks the work. SQLite stores it. Streamlit
shows it. Gemini never decides what you should do; it translates your material
into rows the application can execute on.

```
messy input ──[gemini]──▶ structured rows ──[scheduler]──▶ ranked, dated plan
                                │
                          your answers ──[gemini]──▶ gaps ──▶ mastery
                                │                              │
                                └──────▶ diagnosis · resources · overlap
```

## Quick start

```bash
pip install -r requirements.txt
```

```bash
streamlit run src/frontend/app.py
```

Then hit **🌱 Load demo semester** in the sidebar.

For live Gemini parsing, copy `.env.example` to `.env` and set `GEMINI_API_KEY`.
Without a key the app runs in **mock mode**: every Gemini call falls back to a
regex/heuristic parser in `src/backend/mock.py`, so the whole demo still works
offline — just visibly worse, and every screen says so.

## What it does

### Part 1 — messy input → an executable semester
- **Syllabus → structure.** Every deadline, assessment weightage, the textbook,
  the topic outline, and a 1–5 difficulty estimate with Gemini's reasoning.
- **Notes → TODOs.** Lecture and tutorial notes become concrete, single-sitting
  tasks. A `??` or an "ask prof" in your own handwriting is treated as signal —
  it's logged as a weak point and pulls related work up the list.
- **Priority ranking.** Deterministic and auditable, *not* a Gemini call:
  ```
  priority = 100 × ( 0.40 × grade_weight
                   + 0.30 × urgency
                   + 0.15 × course_difficulty
                   + 0.15 × weakness )
  ```
- **Manual difficulty override.** Gemini guesses; you correct it; every priority
  recomputes. Your override is never clobbered by a re-ingest.
- **Life Compiler.** "I work Mon and Wed 9–5 and want 2 hours a night" compiles
  into busy blocks, deadlines and capacity settings — then into dated calendar
  blocks, each with a one-line reason attached.
- **Workload forecast.** Work owed per day vs. your honest capacity, so an
  impossible week shows up before it arrives.

### Part 2 — the feedback loop
- **Question generation** grounded in your own lecture/tutorial content, biased
  toward your recorded weak points.
- **Mock test grading** with partial credit. Each mistake gets a reusable
  misconception tag (`confuses big-O with big-Theta`), not just a score.
- **Retake detection.** Gemini is told when you've sat a question before and
  marks for genuine understanding rather than recall of its own feedback.
- **Mastery map** via an exponential moving average, so recent evidence
  dominates but history still counts. Low mastery feeds straight back into the
  priority formula.
- **Diagnosis** across every attempt, with evidence and a concrete next action.
- **Resources** matched to your *stated study habits*, not just the topic — if
  you said videos put you to sleep, you get practice sets.
- **Overlap detection.** Where two courses teach the same idea, study it once
  and count it twice.

## Layout

```
src/
  backend/
    app.py        service layer — the only thing the frontend imports
    gemini.py     every Gemini call + its JSON schema
    mock.py       offline heuristic fallback for each of those calls
    scheduler.py  priority scoring + workload forecast (no LLM)
    db.py         SQLite schema and helpers
    seed.py       demo data, loaded through the real pipeline
  frontend/
    app.py        Streamlit UI — no business logic
data/
  samples/        sample syllabi and notes used by the demo button
  dap4y.db        created on first run (gitignored)
```

Two rules keep it honest: the frontend contains no business logic, and the
ranking contains no LLM. Everything Gemini returns is schema-validated before it
touches the database, and every screen labels whether what you're reading came
from Gemini or from the offline fallback.

## Config

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Live parsing. Absent ⇒ mock mode. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model id. |
| `DAP4Y_DB` | `data/dap4y.db` | SQLite path. |

## Known limits

This is a hackathon prototype.

- No auth and no multi-user support — one student per database file.
- Recommended resources are search queries and textbook chapters, not verified
  links; Gemini is told not to guess URLs, but check them anyway.
- Grading is an LLM's opinion. Useful for finding gaps, not for predicting marks.
- Mock mode's parsers are genuinely weak — they're a safety net for a dead wifi
  connection, not a feature.
