"""DAP4Y -- Dynamic Assistant Professor 4 You.

Streamlit frontend. Contains no business logic: every button calls into
``src.backend.app``.

Run from the repo root:   streamlit run src/frontend/app.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.backend import app as api  # noqa: E402
from src.backend import gemini, seed  # noqa: E402
from src.backend.gemini import Attachment  # noqa: E402

st.set_page_config(page_title="DAP4Y", page_icon="🎓", layout="wide")

MIME = {
    "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "webp": "image/webp", "txt": "text/plain",
    "md": "text/markdown", "csv": "text/csv", "mp3": "audio/mpeg",
    "wav": "audio/wav", "m4a": "audio/mp4",
}
UPLOAD_TYPES = list(MIME)


def to_attachments(files) -> list[Attachment]:
    out = []
    for f in files or []:
        ext = f.name.rsplit(".", 1)[-1].lower()
        out.append(Attachment(f.name, MIME.get(ext, "application/octet-stream"), f.getvalue()))
    return out


def source_badge(source: str) -> None:
    if source == "gemini":
        st.caption("✅ Parsed by Gemini")
    elif source.startswith("skipped"):
        st.caption("⏭️ Skipped")
    else:
        st.caption(f"⚠️ {source} — offline heuristics, results are degraded")


def pill(text: str, colour: str = "#444") -> str:
    return (f"<span style='background:{colour};color:#fff;padding:2px 8px;"
            f"border-radius:10px;font-size:0.75rem'>{text}</span>")


api.bootstrap()

# --------------------------------------------------------------------------
# sidebar
# --------------------------------------------------------------------------
with st.sidebar:
    st.title("🎓 DAP4Y")
    st.caption("Dynamic Assistant Professor 4 You")

    if gemini.live():
        st.success(gemini.status(), icon="🟢")
    else:
        st.warning(gemini.status(), icon="🟡")
        st.caption("Copy `.env.example` to `.env` and add `GEMINI_API_KEY` for live parsing.")

    s = api.stats()
    c1, c2 = st.columns(2)
    c1.metric("Courses", s["courses"])
    c2.metric("Deadlines", s["assessments"])
    c1.metric("Open TODOs", s["open_todos"])
    c2.metric("Attempts", s["attempts"])

    st.divider()
    if st.button("🌱 Load demo semester", use_container_width=True):
        with st.spinner("Running the sample files through the real pipeline…"):
            st.session_state["seed_report"] = seed.seed(reset=True)
        st.rerun()

    with st.expander("⚠️ Danger zone"):
        if st.button("Wipe database", use_container_width=True):
            api.reset()
            st.session_state.clear()
            st.rerun()
        st.download_button("Export state (JSON)", api.export_state(),
                           file_name="dap4y_state.json", mime="application/json",
                           use_container_width=True)

if st.session_state.pop("seed_report", None):
    st.toast("Demo semester loaded", icon="🌱")

tabs = st.tabs([
    "📥 Intake", "📊 Priorities", "🗓️ Schedule", "🧠 Study & Quiz",
    "🩺 Coaching", "⚙️ Profile",
])

# ==========================================================================
# 1. INTAKE  -- "Explain My Mess"
# ==========================================================================
with tabs[0]:
    st.header("Give Gemini your mess")
    st.caption("Syllabi, portal screenshots, lecture notes, a voice memo, or one "
               "sentence about your week. Gemini turns all of it into rows in the database.")

    intake = st.radio("What are you handing over?",
                      ["Syllabus / course outline", "Lecture or tutorial notes",
                       "Plain-language week ('Life Compiler')", "Past / mock test"],
                      horizontal=True, label_visibility="collapsed")

    # ---- syllabus -------------------------------------------------------
    if intake == "Syllabus / course outline":
        st.subheader("Syllabus → deadlines, weightages, textbook, difficulty")
        col_a, col_b = st.columns([3, 2])
        with col_a:
            text = st.text_area("Paste the syllabus", height=260, key="syl_text",
                                placeholder="Paste anything — a course outline, a wall "
                                            "of portal text, a forwarded email…")
        with col_b:
            files = st.file_uploader("…or drop files", type=UPLOAD_TYPES,
                                     accept_multiple_files=True, key="syl_files")
            samples = seed.load_samples()
            pick = st.selectbox("Or load a sample", ["—", *samples])
            if pick != "—" and st.button("Use this sample"):
                st.session_state["syl_text"] = samples[pick]
                st.rerun()

        if st.button("🚀 Extract semester", type="primary", disabled=not (text or files)):
            with st.spinner("Gemini is reading the syllabus…"):
                res = api.ingest_syllabus(text, to_attachments(files),
                                          filename=", ".join(f.name for f in files or []))
            source_badge(res["source"])
            c = res["course"]
            st.success(f"**{c['code']} — {c['name']}** · {res['created']['assessments']} graded "
                       f"items · {res['created']['topics']} topics · "
                       f"{res['created']['todos']} TODOs created")
            for w in res.get("warnings", []):
                st.warning(w)
            raw_course = res["raw"].get("course", {})
            if raw_course.get("difficulty_reason"):
                st.info(f"**Difficulty {raw_course.get('difficulty')}/5** — "
                        f"{raw_course['difficulty_reason']}  \n"
                        "_Override this on the Priorities tab if Gemini read you wrong._")
            if res["raw"].get("assessments"):
                st.dataframe(pd.DataFrame(res["raw"]["assessments"]),
                             use_container_width=True, hide_index=True)
            with st.expander("Show the raw JSON Gemini returned"):
                st.json(res["raw"])

    # ---- notes ----------------------------------------------------------
    elif intake == "Lecture or tutorial notes":
        st.subheader("Notes → topics + the TODOs that fill a study session")
        courses = api.list_courses()
        if not courses:
            st.info("Add a syllabus first so there is a course to attach notes to.")
        else:
            course = st.selectbox("Course", courses, format_func=lambda c: f"{c['code']} — {c['name']}")
            col_a, col_b = st.columns([3, 2])
            with col_a:
                text = st.text_area("Paste your notes", height=260, key="note_text",
                                    placeholder="Messy is fine. '??' and 'ask prof' are "
                                                "signal, not noise.")
            with col_b:
                files = st.file_uploader("…or drop files / a whiteboard photo",
                                         type=UPLOAD_TYPES, accept_multiple_files=True,
                                         key="note_files")
                samples = {k: v for k, v in seed.load_samples().items() if "notes" in k}
                pick = st.selectbox("Or load a sample", ["—", *samples])
                if pick != "—" and st.button("Use this sample"):
                    st.session_state["note_text"] = samples[pick]
                    st.rerun()

            if st.button("🚀 Extract TODOs", type="primary", disabled=not (text or files)):
                with st.spinner("Gemini is reading your notes…"):
                    res = api.ingest_notes(course["id"], text, to_attachments(files),
                                           filename=", ".join(f.name for f in files or []))
                source_badge(res["source"])
                created = res["created"]
                st.success(f"{len(created.get('topics', []))} topics · "
                           f"{len(created.get('todos', []))} TODOs")
                if created.get("flagged_confusions"):
                    st.error("**Gemini spotted where you got lost:**\n\n" +
                             "\n".join(f"- {c}" for c in created["flagged_confusions"]))
                    st.caption("These become weak points and pull related work up the priority list.")
                if created.get("todos"):
                    st.dataframe(pd.DataFrame(created["todos"]),
                                 use_container_width=True, hide_index=True)

    # ---- intent ---------------------------------------------------------
    elif intake == "Plain-language week ('Life Compiler')":
        st.subheader("One sentence → structured constraints")
        st.caption("Gemini isn't answering you here. It's compiling your sentence into "
                   "function calls: busy blocks, deadlines, capacity.")
        text = st.text_area(
            "Describe your week", height=140,
            value="I have a stats midterm next Tuesday, I work Monday and Wednesday "
                  "9-5, and I can study about 2 hours a day in the evening.")
        if st.button("🚀 Compile", type="primary", disabled=not text):
            with st.spinner("Compiling…"):
                res = api.ingest_intent(text)
            source_badge(res["source"])
            p = res["payload"]
            st.info(p.get("echo", ""))
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("**Busy blocks**")
                st.dataframe(pd.DataFrame(p.get("busy_blocks", []) or [{"—": "none found"}]),
                             use_container_width=True, hide_index=True)
                st.session_state["busy_blocks"] = p.get("busy_blocks", [])
            with c2:
                st.markdown("**Deadlines**")
                st.dataframe(pd.DataFrame(p.get("deadlines", []) or [{"—": "none found"}]),
                             use_container_width=True, hide_index=True)
                if res["deadlines_added"]:
                    st.success(f"{res['deadlines_added']} deadline(s) matched a known course "
                               "and were saved.")
                else:
                    st.caption("Deadlines are only saved when the course code matches one "
                               "you've added.")
            st.markdown("**Preferences written to your profile**")
            st.json(p.get("preferences", {}))

    # ---- past paper -----------------------------------------------------
    else:
        st.subheader("Past / mock test → a gradeable question bank")
        courses = api.list_courses()
        if not courses:
            st.info("Add a syllabus first.")
        else:
            course = st.selectbox("Course", courses, format_func=lambda c: c["code"],
                                  key="pp_course")
            topics = api.list_topics(course["id"])
            topic = st.selectbox("Topic (optional)", [None, *topics],
                                 format_func=lambda t: "— any —" if t is None else t["name"])
            text = st.text_area("Paste the paper", height=200, key="pp_text")
            files = st.file_uploader("…or upload the PDF / photos", type=UPLOAD_TYPES,
                                     accept_multiple_files=True, key="pp_files")
            if st.button("🚀 Import paper", type="primary", disabled=not (text or files)):
                with st.spinner("Transcribing and normalising questions…"):
                    res = api.add_past_paper(course["id"],
                                             topic["id"] if topic else None,
                                             text, to_attachments(files))
                source_badge(res["source"])
                st.success(f"{len(res['question_ids'])} questions imported. "
                           "Sit them on the Study & Quiz tab.")

    st.divider()
    with st.expander("📜 Ingestion log — everything Gemini has parsed"):
        log = api.ingest_log()
        if not log:
            st.caption("Nothing yet.")
        for row in log:
            st.markdown(f"**{row['kind']}** · {row['created_at']} · {row['filename'] or '—'}  \n"
                        f"{row['summary']}")

# ==========================================================================
# 2. PRIORITIES
# ==========================================================================
with tabs[1]:
    st.header("What to do next, and why")
    courses = api.list_courses()
    if not courses:
        st.info("Nothing here yet — load the demo semester from the sidebar, or add a syllabus.")
    else:
        st.subheader("Course difficulty")
        st.caption("Gemini estimates difficulty from the syllabus. You know better — "
                   "override it here and every priority recomputes.")
        cols = st.columns(min(len(courses), 4))
        for i, c in enumerate(courses):
            with cols[i % len(cols)]:
                st.markdown(f"**{c['code']}**  \n<small>{c['name'][:40]}</small>",
                            unsafe_allow_html=True)
                new = st.slider("difficulty", 1, 5, int(c["difficulty"]),
                                key=f"diff_{c['id']}", label_visibility="collapsed")
                st.caption("✍️ yours" if c["difficulty_source"] == "manual" else "🤖 Gemini's guess")
                if new != c["difficulty"]:
                    api.set_difficulty(c["id"], new)
                    st.rerun()

        st.divider()
        st.subheader("Ranked TODOs")
        with st.expander("How this number is computed"):
            st.code(
                "priority = 100 x ( 0.40 x grade_weight\n"
                "                 + 0.30 x urgency\n"
                "                 + 0.15 x course_difficulty\n"
                "                 + 0.15 x weakness )",
                language="text")
            st.caption("Plain arithmetic, not a Gemini call — so you can argue with it. "
                       "Gemini supplies the inputs (weightages, deadlines, difficulty, "
                       "weak points); the ranking is auditable.")

        todos = api.list_todos("open")
        if not todos:
            st.success("Nothing open. Either you're done or you haven't ingested anything.")
        for t in todos[:40]:
            with st.container(border=True):
                c1, c2, c3 = st.columns([7, 2, 1])
                with c1:
                    due = f" · due {t['due_date']}" if t.get("due_date") else ""
                    st.markdown(
                        f"{pill(t['course_code'], t['colour'])} **{t['title']}**"
                        f"<br><small>{t['detail'][:140]}</small>"
                        f"<br><small style='opacity:.7'>{t['priority_why']}{due}</small>",
                        unsafe_allow_html=True)
                with c2:
                    st.metric("priority", f"{t['priority']:.0f}")
                    st.caption(f"~{t['est_minutes']} min")
                with c3:
                    if st.button("✓", key=f"done_{t['id']}", help="Mark done"):
                        api.set_todo_status(t["id"], "done")
                        st.rerun()

        st.divider()
        st.subheader("Workload forecast")
        st.caption("Work owed per day vs. the capacity in your profile. Red bars are weeks "
                   "that were never going to happen.")
        fc = pd.DataFrame(api.forecast())
        if not fc.empty:
            fc = fc.set_index("date")
            st.bar_chart(fc[["required_minutes", "capacity_minutes"]])
            over = fc[fc["overloaded"]]
            if not over.empty:
                st.error(f"{len(over)} day(s) over capacity: {', '.join(over.index[:5])}"
                         f"{'…' if len(over) > 5 else ''}. Drop something, or raise your "
                         "daily capacity honestly.")

        st.divider()
        st.subheader("All deadlines")
        a = api.list_assessments()
        if a:
            df = pd.DataFrame(a)[["course_code", "title", "kind", "due_date", "weight_pct", "notes"]]
            st.dataframe(df, use_container_width=True, hide_index=True)
            by_course = df.groupby("course_code")["weight_pct"].sum()
            bad = by_course[(by_course - 100).abs() > 1]
            for code, total in bad.items():
                st.warning(f"{code}: weights sum to {total:g}%, not 100% — a graded item "
                           "may have been missed.")

# ==========================================================================
# 3. SCHEDULE
# ==========================================================================
with tabs[2]:
    st.header("Your calendar")
    st.caption("Ranked TODOs + your real-life constraints → dated, timed blocks with a "
               "reason attached to each one.")

    c1, c2, c3 = st.columns([1, 1, 2])
    horizon = c1.number_input("Days ahead", 1, 21, 7)
    replace = c2.checkbox("Replace existing plan", value=True)
    busy = st.session_state.get("busy_blocks", [])
    c3.caption(f"{len(busy)} busy block(s) carried over from the Life Compiler."
               if busy else "No busy blocks — compile a sentence on the Intake tab to add some.")

    if st.button("🗓️ Build schedule", type="primary"):
        with st.spinner("Planning…"):
            res = api.build_schedule(int(horizon), busy_blocks=busy, replace=replace)
        source_badge(res["source"])
        st.success(f"{res['planned']} blocks planned.")
        p = res["payload"]
        if p.get("constraints_understood"):
            st.info("**Constraints applied:** " + " · ".join(p["constraints_understood"]))
        for t in p.get("tradeoffs", []):
            st.warning(t)

    sessions = api.list_sessions()
    if not sessions:
        st.info("No schedule yet.")
    else:
        by_date: dict[str, list] = {}
        for s in sessions:
            by_date.setdefault(s["date"], []).append(s)
        for d, blocks in list(by_date.items())[:14]:
            try:
                label = datetime.fromisoformat(d).strftime("%A %d %b")
            except ValueError:
                label = d
            total = sum(1 for b in blocks)
            st.markdown(f"#### {label}  <small style='opacity:.6'>{total} block(s)</small>",
                        unsafe_allow_html=True)
            for b in blocks:
                with st.container(border=True):
                    c1, c2 = st.columns([8, 2])
                    with c1:
                        code = b.get("course_code") or "general"
                        st.markdown(
                            f"`{b['start_time']}–{b['end_time']}` "
                            f"{pill(code, b.get('colour') or '#555')} **{b['focus']}**"
                            f"<br><small style='opacity:.75'>{b['rationale']}</small>",
                            unsafe_allow_html=True)
                    with c2:
                        if b["status"] == "planned":
                            if st.button("Done", key=f"sess_{b['id']}", use_container_width=True):
                                api.set_session_status(b["id"], "done")
                                st.rerun()
                            if st.button("Skip", key=f"skip_{b['id']}", use_container_width=True):
                                api.set_session_status(b["id"], "skipped")
                                st.rerun()
                        else:
                            st.caption(f"— {b['status']}")

# ==========================================================================
# 4. STUDY & QUIZ
# ==========================================================================
with tabs[3]:
    st.header("Study session")
    courses = api.list_courses()
    if not courses:
        st.info("Load a semester first.")
    else:
        left, right = st.columns([1, 2])

        with left:
            st.subheader("Generate")
            course = st.selectbox("Course", courses, format_func=lambda c: c["code"], key="q_course")
            topics = api.list_topics(course["id"])
            topic = st.selectbox("Topic", [None, *topics],
                                 format_func=lambda t: "— whole course —" if t is None else t["name"])
            n = st.slider("Questions", 3, 12, 5)
            diff = st.slider("Difficulty", 1, 5, 3)
            if st.button("✨ Generate questions", type="primary", use_container_width=True):
                with st.spinner("Writing questions from your own material…"):
                    res = api.make_quiz(course["id"], topic["id"] if topic else None, n, diff)
                st.session_state["quiz_ids"] = res["question_ids"]
                st.session_state["quiz_source"] = res["source"]
                st.session_state["quiz_weak"] = res["targeted_weak_points"]
                st.session_state.pop("quiz_result", None)
                st.rerun()

            st.divider()
            st.subheader("Or retake")
            bank = api.list_questions(course["id"])
            attempted = [q for q in bank if q["attempts"] > 0]
            if attempted:
                st.caption(f"{len(attempted)} question(s) you've already sat.")
                if st.button("🔁 Retake your weakest 5", use_container_width=True):
                    worst = sorted(
                        attempted,
                        key=lambda q: (api.db.query_one(
                            "SELECT AVG(score) s FROM attempt WHERE question_id = ?",
                            [q["id"]]) or {"s": 1})["s"] or 1)[:5]
                    st.session_state["quiz_ids"] = [q["id"] for q in worst]
                    st.session_state["quiz_source"] = "retake"
                    st.session_state["quiz_weak"] = []
                    st.session_state.pop("quiz_result", None)
                    st.rerun()
            else:
                st.caption("No attempts yet.")

        with right:
            qids = st.session_state.get("quiz_ids", [])
            if not qids:
                st.info("Generate a set on the left, or import a past paper on the Intake tab.")
            else:
                weak = st.session_state.get("quiz_weak") or []
                if weak:
                    st.caption("🎯 Biased toward your recorded weak points: " + ", ".join(weak))
                questions = api.get_questions(qids)
                with st.form("quiz"):
                    answers = {}
                    for i, q in enumerate(questions, 1):
                        st.markdown(f"**Q{i}.** {q['prompt']}  "
                                    f"<small style='opacity:.6'>(difficulty {q['difficulty']}/5)"
                                    f"</small>", unsafe_allow_html=True)
                        opts = api.db.jload(q["options"], [])
                        if q["kind"] == "mcq" and opts:
                            answers[q["id"]] = st.radio("answer", opts, key=f"a_{q['id']}",
                                                        label_visibility="collapsed", index=None)
                        else:
                            answers[q["id"]] = st.text_area("answer", key=f"a_{q['id']}",
                                                            height=90, label_visibility="collapsed")
                    if st.form_submit_button("📝 Submit for marking", type="primary"):
                        with st.spinner("Marking…"):
                            st.session_state["quiz_result"] = api.submit_quiz(
                                {k: (v or "") for k, v in answers.items()})
                        st.rerun()

                result = st.session_state.get("quiz_result")
                if result:
                    st.divider()
                    source_badge(result["source"])
                    scores = [r["score"] for r in result.get("results", [])]
                    if scores:
                        c1, c2 = st.columns(2)
                        c1.metric("Score", f"{sum(scores) / len(scores):.0%}")
                        c2.metric("Sitting", "Retake" if result.get("is_retake") else "First attempt")
                    st.info(result.get("overall", ""))
                    q_by_id = {q["id"]: q for q in questions}
                    for r in result.get("results", []):
                        q = q_by_id.get(r.get("question_id"))
                        if not q:
                            continue
                        icon = {"correct": "✅", "partial": "🟡",
                                "incorrect": "❌", "blank": "⬜"}.get(r.get("verdict"), "•")
                        with st.expander(f"{icon} {r.get('score', 0):.0%} — {q['prompt'][:80]}"):
                            st.markdown(f"**Feedback.** {r.get('feedback', '')}")
                            if r.get("gaps"):
                                st.markdown("**Gaps recorded:** " +
                                            ", ".join(f"`{g}`" for g in r["gaps"]))
                            st.markdown(f"**Model answer.** {q['answer']}")
                    st.caption("Every gap above is now a weak point — it raises the priority "
                               "of related TODOs and steers your next question set.")

# ==========================================================================
# 5. COACHING
# ==========================================================================
with tabs[4]:
    st.header("What you're struggling with")
    if not api.list_courses():
        st.info("Load a semester first.")
    else:
        mastery = api.list_mastery()
        if mastery:
            st.subheader("Mastery map")
            df = pd.DataFrame(mastery)[["course_code", "label", "score", "samples", "updated_at"]]
            df["score"] = (df["score"] * 100).round()  # ProgressColumn formats the raw value
            st.dataframe(
                df, use_container_width=True, hide_index=True,
                column_config={"score": st.column_config.ProgressColumn(
                    "mastery", min_value=0, max_value=100, format="%d%%")})
        else:
            st.caption("No evidence yet — sit a quiz or ingest notes with confusions in them.")

        st.divider()
        c1, c2, c3 = st.columns(3)
        if c1.button("🩺 Diagnose", use_container_width=True, type="primary"):
            with st.spinner("Reading across every attempt…"):
                st.session_state["diagnosis"] = api.run_diagnosis()
        if c2.button("📚 Recommend resources", use_container_width=True):
            with st.spinner("Matching resources to your habits…"):
                st.session_state["resources"] = api.run_resources()
        if c3.button("🔗 Find overlap", use_container_width=True):
            with st.spinner("Comparing topics across courses…"):
                st.session_state["overlaps"] = api.run_overlaps()

        d = st.session_state.get("diagnosis")
        if d:
            st.subheader("Diagnosis")
            source_badge(d["source"])
            if d.get("coach_note"):
                st.info(d["coach_note"])
            for s in d.get("struggles", []):
                colour = {"high": "#b85450", "medium": "#d79b00"}.get(s.get("severity"), "#666")
                with st.container(border=True):
                    st.markdown(f"{pill(s.get('severity', ''), colour)} "
                                f"**{s.get('label', '')}** · {s.get('course_code', '')}",
                                unsafe_allow_html=True)
                    st.markdown(f"<small>**Evidence.** {s.get('evidence', '')}</small>",
                                unsafe_allow_html=True)
                    st.markdown(f"**Do this.** {s.get('fix', '')}")
            if d.get("strengths"):
                st.success("**Holding up fine:** " + ", ".join(d["strengths"]))

        r = st.session_state.get("resources")
        if r:
            st.subheader("Resources")
            source_badge(r["source"])
            if r.get("note"):
                st.caption(r["note"])
            st.caption("Matched to your stated study habits, not just to the topic.")
            for res in r.get("resources", []):
                with st.container(border=True):
                    st.markdown(f"{pill(res.get('kind', ''), '#3d8b8b')} "
                                f"**{res.get('title', '')}** · {res.get('course_code', '')} "
                                f"→ _{res.get('label', '')}_", unsafe_allow_html=True)
                    st.markdown(f"`{res.get('locator', '')}`")
                    st.caption(res.get("why", ""))

        o = st.session_state.get("overlaps")
        if o:
            st.subheader("Overlap between courses")
            source_badge(o["source"])
            if o.get("note"):
                st.caption(o["note"])
            if not o.get("overlaps"):
                st.caption("No overlap found. The offline parser only matches repeated "
                           "words — real conceptual overlap needs a live Gemini key.")
            total = sum(int(x.get("saved_minutes") or 0) for x in o.get("overlaps", []))
            if total:
                st.success(f"Studying these once instead of twice saves roughly "
                           f"**{total // 60}h {total % 60}m**.")
            for x in o.get("overlaps", []):
                with st.container(border=True):
                    st.markdown(f"**{x.get('label', '')}** — "
                                f"{' ↔ '.join(x.get('course_codes', []))}")
                    st.caption(x.get("payoff", ""))

        st.divider()
        with st.expander("📈 Attempt history"):
            attempts = api.list_attempts(50)
            if attempts:
                st.dataframe(
                    pd.DataFrame(attempts)[["created_at", "course_code", "topic_name",
                                            "prompt", "score", "attempt_no", "gaps"]],
                    use_container_width=True, hide_index=True)
            else:
                st.caption("No attempts yet.")

# ==========================================================================
# 6. PROFILE
# ==========================================================================
with tabs[5]:
    st.header("Profile")
    st.caption("This is the 'study habits' and 'preferred study time' input from the "
               "blackboard. It changes grading tone, resource format and block placement.")
    student = api.get_student()
    with st.form("profile"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", student.get("name", ""))
        term = c2.text_input("Term", student.get("term", ""))
        habits = st.text_area(
            "How do you actually study?", student.get("study_habits", ""), height=140,
            placeholder="Be honest — 'I cram', 'I avoid the courses I'm worst at', "
                        "'videos put me to sleep'. Gemini uses this verbatim.")
        windows = st.text_area(
            "Preferred study windows (one per line)",
            "\n".join(student.get("preferred_windows", [])), height=100,
            placeholder="weekday evenings 19:00-23:00\nSunday afternoons")
        c1, c2, c3 = st.columns(3)
        cap = c1.number_input("Daily capacity (min)", 30, 720,
                              int(student.get("daily_capacity_min", 180)), step=30)
        sess = c2.number_input("Session length (min)", 20, 240,
                               int(student.get("session_minutes", 90)), step=15)
        brk = c3.number_input("Break (min)", 0, 60, int(student.get("break_minutes", 15)), step=5)
        if st.form_submit_button("Save", type="primary"):
            api.save_student(
                name=name, term=term, study_habits=habits,
                preferred_windows=[w.strip() for w in windows.splitlines() if w.strip()],
                daily_capacity_min=int(cap), session_minutes=int(sess), break_minutes=int(brk))
            api.recompute_priorities()
            st.success("Saved.")

    st.divider()
    st.subheader("Courses")
    for c in api.list_courses():
        with st.container(border=True):
            c1, c2 = st.columns([8, 1])
            c1.markdown(f"**{c['code']} — {c['name']}**  \n"
                        f"<small>{c['instructor'] or 'no instructor'} · "
                        f"{c['textbook'] or 'no textbook'} · difficulty {c['difficulty']}/5 "
                        f"({c['difficulty_source']})</small>", unsafe_allow_html=True)
            if c2.button("🗑", key=f"del_{c['id']}", help="Delete course and everything under it"):
                api.delete_course(c["id"])
                st.rerun()
