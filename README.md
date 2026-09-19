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

### Pre-planning — the term skeleton

The planning stage builds **empty** study blocks. Assigning actual tasks to them
is a separate stage, so nothing here invents work.

1. **Syllabus → courses.** Gemini extracts deadlines, weightages and topics.
2. **Course → difficulty, from uoftindex.ca.** DAP4Y queries UofT Index for the
   course's drop rate, student-rated workload, bird count, ratings and review
   count, then hands that one payload to Gemini. The model has **no web access**
   and is instructed to use nothing but the payload — so "only consult
   uoftindex.ca" is enforced by construction, not by asking nicely. It returns a
   1–5 difficulty, a confidence, the exact fields it used, and a weekly study-hour
   estimate.
3. **You declare your availability.** One window per weekday — the hours you
   could actually study. Edit it in the table on the Plan tab.
4. **→ Gemini lays out the term.** It splits your weekly capacity across courses
   (harder courses get a bigger share, justified per course), then places blocks
   inside those windows.

> **Why we don't point Gemini's `url_context` tool at the site.** uoftindex.ca is
> a client-rendered SPA: fetching the course URL server-side returns an empty
> JavaScript shell with zero course data. The real numbers come from the site's
> own GraphQL endpoint, which is what `uoftindex.py` calls. Responses are cached
> for 7 days so a demo doesn't hammer a volunteer-run site.

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
    app.py            service layer — the only thing the frontend imports
    gemini.py         every Gemini call + its JSON schema
    uoftindex.py      UofT Index GraphQL client (the only external data source)
    mock.py           offline fallback for the PARSING calls only
    scheduler.py      priority scoring + workload forecast (no LLM)
    db.py             SQLite schema and helpers
    seed.py           demo data, loaded through the real pipeline
  frontend/
    app.py            Streamlit UI — no business logic
scripts/
  verify_gemini.py    exercises every Gemini call, reports live vs. fallback
data/
  samples/            sample syllabi and notes used by the demo button
  cache/uoftindex/    cached course lookups (gitignored)
  dap4y.db            created on first run (gitignored)
```

Check the live path after adding a key:

```bash
python scripts/verify_gemini.py
```

Two rules keep it honest: the frontend contains no business logic, and the
ranking contains no LLM. Everything Gemini returns is schema-validated before it
touches the database, and every screen labels whether what you're reading came
from Gemini or from the offline fallback.

## Config

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Live parsing. Absent ⇒ mock mode. |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Model id. |
| `DAP4Y_DB` | `data/dap4y.db` | SQLite path. |

## Known limits

This is a hackathon prototype.

- No auth and no multi-user support — one student per database file.
- Recommended resources are search queries and textbook chapters, not verified
  links; Gemini is told not to guess URLs, but check them anyway.
- Grading is an LLM's opinion. Useful for finding gaps, not for predicting marks.
- Mock mode's parsers are genuinely weak — they're a safety net for a dead wifi
  connection, not a feature.
- **Scheduling has no offline fallback, on purpose.** A fabricated timetable that
  looks real is worse than an error, so `plan_blocks` fails loudly without a key.
- Single-user: one `student` row, no accounts, no Google Calendar integration.
  Availability is typed in by hand.
