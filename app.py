"""StudyLink -- Streamlit interface.

    streamlit run app.py

Four views: a course dashboard, a notes browser, the assignment work session, and
the retrieval-quality view. The last one is deliberately part of the product, not
a hidden script: a student should be able to see how well the matching is doing
before trusting it.
"""

from __future__ import annotations

import streamlit as st

from studylink import store
from studylink.agent import AgentUnavailable, citation_coverage, resolve_citations
from studylink.canvas import CanvasError
from studylink.config import load_settings
from studylink.evaluation.runner import format_sweep
from studylink.models import NoteMatch
from studylink.service import StudyLink

st.set_page_config(page_title="StudyLink", page_icon="\U0001f4da", layout="wide")


@st.cache_resource
def get_app() -> StudyLink:
    return StudyLink(load_settings())


app = get_app()


# ------------------------------------------------------------------ shared bits


def confidence_badge(match) -> str:
    if match.confidence >= 0.7:
        return "\U0001f7e2 strong"
    if match.confidence >= 0.4:
        return "\U0001f7e1 moderate"
    return "\U0001f534 weak"


def render_match(match: NoteMatch, key_prefix: str) -> None:
    """One retrieved note, with the evidence that explains why it matched."""
    header = f"{confidence_badge(match)} · {match.note.title}"
    with st.expander(header, expanded=False):
        left, right = st.columns([3, 1])
        with right:
            st.metric("Confidence", f"{match.confidence:.2f}")
            st.caption(f"cosine {match.score:.3f}")
            st.caption(f"course: {match.note.course_name or 'unassigned'}")
            st.caption(f"type: {match.note.source_type}")
        with left:
            st.markdown("**Why this matched**")
            if match.evidence.overlapping_terms:
                st.markdown(
                    "Shared concepts: "
                    + " ".join(f"`{term}`" for term in match.evidence.overlapping_terms)
                )
            else:
                st.caption(
                    "No shared vocabulary -- this is a purely semantic match. Worth checking."
                )
            st.markdown(f"> {match.evidence.snippet}")
            st.caption(f"from chunk #{match.evidence.chunk_ordinal} of this note")

        if st.button("Show full note", key=f"{key_prefix}_full_{match.note.id}"):
            st.text_area(
                "Note body",
                match.note.body,
                height=300,
                key=f"{key_prefix}_body_{match.note.id}",
            )


def sidebar() -> None:
    status = app.status()
    st.sidebar.title("StudyLink")

    st.sidebar.caption(f"embedding provider: `{status.provider}`")
    cols = st.sidebar.columns(2)
    cols[0].metric("Assignments", status.assignments)
    cols[1].metric("Notes", status.notes)

    if status.index_stale:
        st.sidebar.warning("Index is out of date with the current settings.")
    if not status.ready_for_matching:
        st.sidebar.info("Nothing indexed yet. Add notes, then reindex.")

    st.sidebar.divider()
    st.sidebar.subheader("Canvas")
    if app.settings.canvas_configured:
        if st.sidebar.button("Sync from Canvas", use_container_width=True):
            with st.spinner("Syncing..."):
                try:
                    result = app.sync_canvas()
                    st.sidebar.success(f"Synced {result.summary}")
                    for error in result.errors[:3]:
                        st.sidebar.caption(f"warning: {error}")
                except CanvasError as exc:
                    st.sidebar.error(str(exc))
                except Exception as exc:  # network, DNS, timeouts
                    st.sidebar.error(f"Sync failed: {exc}")
    else:
        st.sidebar.caption("Not configured. Set CANVAS_API_URL and CANVAS_API_TOKEN.")

    last = status.last_sync
    if last:
        st.sidebar.caption(f"last sync: {last['ran_at']}")

    st.sidebar.divider()
    st.sidebar.subheader("Retrieval settings")
    config = app.config
    top_k = st.sidebar.slider("Notes per assignment (k)", 1, 10, config.top_k)
    threshold = st.sidebar.slider(
        "Score threshold", 0.0, 0.6, float(config.score_threshold), step=0.01
    )
    chunk_size = st.sidebar.select_slider(
        "Chunk size (words)", options=[80, 120, 180, 300, 450], value=config.chunk_size
    )
    chunk_overlap = st.sidebar.select_slider(
        "Chunk overlap (words)", options=[0, 20, 40, 80], value=config.chunk_overlap
    )

    changed = (
        top_k != config.top_k
        or threshold != config.score_threshold
        or chunk_size != config.chunk_size
        or chunk_overlap != config.chunk_overlap
    )
    if changed:
        app.set_retrieval_config(
            config.with_(
                top_k=top_k,
                score_threshold=threshold,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )
        if chunk_size != config.chunk_size or chunk_overlap != config.chunk_overlap:
            st.sidebar.caption("Chunking changed -- reindex to apply.")

    if st.sidebar.button("Reindex", use_container_width=True):
        with st.spinner("Reindexing..."):
            stats = app.reindex()
        st.sidebar.success(stats.summary)


# ---------------------------------------------------------------------- views


def view_dashboard() -> None:
    st.header("Courses and assignments")
    courses = app.list_courses()
    if not courses:
        st.info(
            "No courses yet. Sync from Canvas, or run `python scripts/seed_demo.py` "
            "to load a demo corpus."
        )
        return

    names = ["All courses"] + [c.name for c in courses]
    choice = st.selectbox("Course", names)
    course_id = None if choice == "All courses" else next(c.id for c in courses if c.name == choice)

    assignments = app.list_assignments(course_id)
    if not assignments:
        st.info("No assignments for this course.")
        return

    for assignment in assignments:
        matches = app.matches_for_assignment(assignment.id)
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                st.subheader(assignment.name)
                meta = [assignment.course_name]
                if assignment.due_at:
                    meta.append(f"due {assignment.due_at[:10]}")
                if assignment.points_possible is not None:
                    meta.append(f"{assignment.points_possible:g} pts")
                st.caption(" · ".join(meta))
                if assignment.description:
                    st.write(assignment.description[:280] + ("..." if len(assignment.description) > 280 else ""))
            with right:
                st.metric("Matched notes", len(matches))
                if st.button("Open work session", key=f"open_{assignment.id}"):
                    st.session_state["work_assignment_id"] = assignment.id
                    st.session_state["view"] = "Work session"
                    st.rerun()

            if matches:
                st.caption(
                    "Top matches: "
                    + ", ".join(f"{m.note.title} ({m.confidence:.2f})" for m in matches[:3])
                )


def view_notes() -> None:
    st.header("Notes")

    with st.expander("Add a note", expanded=False):
        courses = app.list_courses()
        course_names = ["(no course)"] + [c.name for c in courses]
        col1, col2 = st.columns([3, 2])
        title = col1.text_input("Title")
        course_choice = col2.selectbox("Course", course_names)
        source_type = col2.radio("Type", ["note", "transcript"], horizontal=True)
        body = st.text_area("Body (markdown or plain text)", height=220)

        uploaded = st.file_uploader("...or upload a .md / .txt file", type=["md", "txt"])
        if uploaded is not None:
            body = uploaded.read().decode("utf-8", errors="replace")
            title = title or uploaded.name.rsplit(".", 1)[0]
            st.caption(f"Loaded {len(body)} characters from {uploaded.name}")

        if st.button("Save note", type="primary"):
            if not body.strip():
                st.error("The note body is empty.")
            else:
                course_id = (
                    None
                    if course_choice == "(no course)"
                    else next(c.id for c in courses if c.name == course_choice)
                )
                note_id = app.add_note(title or "Untitled note", body, course_id, source_type)
                st.success(f"Saved note #{note_id} and reindexed.")
                st.rerun()

    col1, col2 = st.columns([3, 2])
    query = col1.text_input("Search notes", placeholder="keyword search, or a concept for semantic search")
    mode = col2.radio("Search mode", ["keyword", "semantic"], horizontal=True)

    if query and mode == "semantic":
        matches = app.search_notes(query, top_k=8)
        st.caption(f"{len(matches)} semantic match(es)")
        for match in matches:
            render_match(match, "search")
        return

    notes = app.list_notes(search=query)
    st.caption(f"{len(notes)} note(s)")
    for note in notes:
        with st.expander(f"{note.title}  ·  {note.course_name or 'unassigned'}"):
            st.caption(f"{note.source_type} · added {note.created_at}")
            st.text(note.body[:1500] + ("..." if len(note.body) > 1500 else ""))

            if st.button("Which assignments is this relevant to?", key=f"rev_{note.id}"):
                reverse = app.assignments_for_note(note.id, top_k=5)
                if not reverse:
                    st.caption("No assignment scored above the threshold.")
                for item in reverse:
                    st.markdown(
                        f"**{item.assignment.name}** — confidence {item.confidence:.2f} "
                        f"(cosine {item.score:.3f})"
                    )
                    if item.evidence.overlapping_terms:
                        st.caption("shared: " + ", ".join(item.evidence.overlapping_terms))

            if st.button("Delete", key=f"del_{note.id}"):
                app.delete_note(note.id)
                st.rerun()


def view_work_session() -> None:
    st.header("Work session")
    assignments = app.list_assignments()
    if not assignments:
        st.info("No assignments yet.")
        return

    ids = [a.id for a in assignments]
    default_id = st.session_state.get("work_assignment_id", ids[0])
    index = ids.index(default_id) if default_id in ids else 0
    assignment = st.selectbox(
        "Assignment",
        assignments,
        index=index,
        format_func=lambda a: f"{a.name}  ({a.course_name})",
    )
    st.session_state["work_assignment_id"] = assignment.id

    matches = app.matches_for_assignment(assignment.id)

    left, right = st.columns([1, 1])

    with left:
        st.subheader("Assignment")
        with st.container(border=True):
            meta = [assignment.course_name]
            if assignment.due_at:
                meta.append(f"due {assignment.due_at[:10]}")
            if assignment.submission_types:
                meta.append(assignment.submission_types)
            st.caption(" · ".join(meta))
            st.write(assignment.description or "(no description)")

        st.subheader(f"Matched notes ({len(matches)})")
        if not matches:
            st.warning(
                "Nothing matched above the score threshold. Lower the threshold in the "
                "sidebar, or add notes for this course."
            )
        for match in matches:
            render_match(match, "work")

    with right:
        st.subheader("Agent output")
        mode = st.radio(
            "What do you want?",
            ["outline", "draft", "summary"],
            horizontal=True,
            format_func=lambda m: {
                "outline": "Study outline",
                "draft": "Draft skeleton",
                "summary": "Concept summary",
            }[m],
        )

        session_key = f"session_{assignment.id}"
        if st.button("Generate", type="primary", disabled=not matches):
            if not app.settings.anthropic_configured:
                st.error("No Anthropic credentials. Set ANTHROPIC_API_KEY to use the agent.")
            else:
                try:
                    with st.spinner("Reading your notes..."):
                        output, messages = app.agent().synthesize(assignment, matches, mode)
                    st.session_state[session_key] = {
                        "messages": messages,
                        "output": output.text,
                        "offered": [m.note.id for m in matches],
                    }
                except AgentUnavailable as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error(f"The agent call failed: {exc}")

        session = st.session_state.get(session_key)
        if session:
            st.markdown(session["output"])

            coverage = citation_coverage(session["output"], session["offered"])
            with st.container(border=True):
                st.caption("Traceability")
                cited = resolve_citations(app.conn, coverage["cited"], app.user_id)
                if cited:
                    for item in cited:
                        marker = "✓" if item["valid"] else "⚠"
                        st.caption(f"{marker} [N{item['note_id']}] {item['title']}")
                else:
                    st.caption("The output cited no notes -- treat it with suspicion.")
                if coverage["hallucinated_ids"]:
                    st.error(
                        "Cited note ids that were never supplied: "
                        f"{coverage['hallucinated_ids']}"
                    )
                if coverage["unused_offered"]:
                    st.caption(f"Matched but unused: {coverage['unused_offered']}")

            st.divider()
            st.caption("Refine")
            for message in session["messages"][1:]:
                if isinstance(message["content"], str):
                    with st.chat_message(message["role"]):
                        st.markdown(message["content"])

            follow_up = st.chat_input("e.g. focus more on the second lecture's example")
            if follow_up:
                try:
                    with st.spinner("Thinking..."):
                        result, messages = app.agent().refine(session["messages"], follow_up)
                    session["messages"] = messages
                    session["output"] = result.text
                    st.session_state[session_key] = session
                    st.rerun()
                except Exception as exc:
                    st.error(f"Refinement failed: {exc}")


def view_quality() -> None:
    st.header("Retrieval quality")
    st.caption(
        "Measured against the hand-labelled pairs in `data/eval/labels.json`. "
        "Numbers here are how you know the matching works, rather than assuming it."
    )

    labels = store.list_labels(app.conn)
    if not labels:
        st.info(
            "No labels loaded. Run `python scripts/seed_demo.py`, or label pairs below."
        )
    else:
        positive = sum(1 for label in labels if label["relevant"])
        cols = st.columns(3)
        cols[0].metric("Labelled pairs", len(labels))
        cols[1].metric("Positive", positive)
        cols[2].metric("Negative", len(labels) - positive)

    if st.button("Evaluate current configuration", type="primary"):
        with st.spinner("Scoring..."):
            report = app.evaluate()
        st.session_state["report"] = report

    report = st.session_state.get("report")
    if report:
        k = report.config.top_k
        cols = st.columns(4)
        cols[0].metric(f"recall@{k}", f"{report.macro.get(f'recall@{k}', 0):.3f}")
        cols[1].metric(f"precision@{k}", f"{report.macro.get(f'precision@{k}', 0):.3f}")
        cols[2].metric("MRR", f"{report.macro.get('mrr', 0):.3f}")
        cols[3].metric(f"nDCG@{k}", f"{report.macro.get(f'ndcg@{k}', 0):.3f}")

        st.caption(
            "precision@k is capped when an assignment has fewer than k relevant notes, so "
            "compare it across configurations rather than reading it as an absolute score."
        )

        if report.unresolved_labels:
            st.warning(
                "Some labels did not resolve to rows in the database -- they are silently "
                "shrinking the eval set:\n\n"
                + "\n".join(f"- {u}" for u in report.unresolved_labels)
            )

        rows = []
        for entry in report.per_assignment:
            m = entry.metrics.as_dict()
            rows.append(
                {
                    "assignment": entry.assignment.name,
                    f"recall@{k}": m[f"recall@{k}"],
                    f"precision@{k}": m[f"precision@{k}"],
                    "mrr": m["mrr"],
                    "relevant": entry.metrics.n_relevant,
                    "false positives in top k": len(entry.false_positives),
                }
            )
        st.dataframe(rows, use_container_width=True)

    st.divider()
    st.subheader("Configuration sweep")
    st.caption("Reindexes for each chunking setting, so this takes a moment.")
    if st.button("Run sweep"):
        with st.spinner("Sweeping..."):
            reports = app.sweep(chunk_sizes=(80, 120, 180, 300), chunk_overlaps=(0, 20, 40))
        st.code(format_sweep(reports))

    st.divider()
    st.subheader("Add a label")
    assignments = app.list_assignments()
    notes = app.list_notes()
    if assignments and notes:
        col1, col2, col3 = st.columns([2, 2, 1])
        assignment = col1.selectbox("Assignment", assignments, format_func=lambda a: a.name)
        note = col2.selectbox("Note", notes, format_func=lambda n: n.title)
        relevant = col3.radio("Should match?", ["yes", "no"], horizontal=True)
        rationale = st.text_input("Rationale (one line -- future you will thank present you)")
        if st.button("Save label"):
            store.set_label(app.conn, assignment.id, note.id, relevant == "yes", rationale)
            st.success("Saved. Export to JSON with `python scripts/export_labels.py`.")
            st.rerun()


# ------------------------------------------------------------------------ main

sidebar()

VIEWS = {
    "Dashboard": view_dashboard,
    "Notes": view_notes,
    "Work session": view_work_session,
    "Retrieval quality": view_quality,
}

default_view = st.session_state.get("view", "Dashboard")
selected = st.radio(
    "view",
    list(VIEWS),
    index=list(VIEWS).index(default_view),
    horizontal=True,
    label_visibility="collapsed",
)
st.session_state["view"] = selected
VIEWS[selected]()
