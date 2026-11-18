
"""CyberOps AI local case dashboard."""
import os
from pathlib import Path

import streamlit as st

from cases import SEVERITIES, STATES, CaseError, Store, suggestions

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="CyberOps AI", layout="wide")
st.html("""
<style>
.stApp,[data-testid="stHeader"] {background:#000;}
.stMainBlockContainer {max-width:74rem;padding-top:3.6rem;}
.st-key-hero,.st-key-case_panel,.st-key-handoff_panel {
    padding:clamp(1rem,3vw,1.85rem);border:1px solid #512B38;border-radius:1rem;background:#130C11;
}
.st-key-hero {background:radial-gradient(ellipse at top right,#5A2034,#090608 70%);}
.st-key-hero h1 {font-size:clamp(2.75rem,7vw,4.5rem);letter-spacing:-.06em;}
[data-testid="stBaseButton-primary"]:not(:disabled) p {color:#2A0B15;font-weight:700;}
[data-testid="stText"] {white-space:pre-wrap;overflow-wrap:anywhere;}
</style>
""")


@st.cache_resource
def store_at(path):
    return Store(path)


store = store_at(str(Path(os.environ.get("CYBEROPS_DB_PATH", ROOT / "data/cyberops.sqlite3")).resolve()))


def intake(raw):
    try:
        with st.spinner("Validating alerts…"):
            st.session_state.import_result = store.import_alerts(raw)
    except CaseError as exc:
        st.session_state.pop("import_result", None)
        st.error(str(exc))


def overview(case, linked, events):
    case_id, version = case["id"], case["version"]
    st.subheader("Case timeline")
    note_count = sum(row["kind"] == "Analyst note" for row in events)
    question_count = sum(row["kind"] == "Open question" for row in events)
    st.caption(f"Linked alerts: {len(linked)} · Analyst notes: {note_count} · Open questions: {question_count}")
    timeline = [{"UTC timestamp": row["at"], "Stage": row["kind"], "Author": row["author"] or "Unknown",
                 "Evidence": row["detail"]} for row in events]
    timeline.extend({"UTC timestamp": row["timestamp"], "Stage": "Alert observed", "Author": row["source"],
                     "Evidence": f"{row['title']} · {row['severity']} · Alert ID {row['id']}"} for row in linked)
    st.dataframe(sorted(timeline, key=lambda row: row["UTC timestamp"]),
                 hide_index=True, use_container_width=True)
    st.subheader("Link evidence")
    known = {row["id"] for row in linked}
    candidates = [row for row in store.alerts() if row["id"] not in known]
    proximity = st.number_input("Suggestion proximity (minutes)", 1, 1440, 30)
    nearby = {row["id"] for row in suggestions(linked, candidates, proximity)}
    st.caption(f"Suggestions require the same entity and an absolute time gap of at most {proximity} minutes "
               "from a linked alert. No links are applied automatically.")
    if linked:
        with st.expander("Linked alert records"):
            st.dataframe([dict(row) for row in linked], hide_index=True, use_container_width=True)
    if candidates:
        options = {row["id"]: f"{row['source']}/{row['external_id']} · {row['title']}"
                   + (" · Suggested" if row["id"] in nearby else "") for row in candidates}
        selected = st.multiselect("Alerts to attach", list(options), format_func=options.__getitem__,
                                  key=f"links_{case_id}_{version}")
        if st.button("Attach selected alerts", disabled=not selected):
            try:
                store.link(case_id, selected)
                st.rerun()
            except CaseError as exc:
                st.error(str(exc))
    else:
        st.info("No unlinked alerts. Import more records to add evidence.")

    with st.form(f"note_{case_id}_{version}"):
        kind = st.selectbox("Entry type", ["Analyst note", "Open question"])
        author = st.text_input("Author", max_chars=100)
        note = st.text_area("Note or question", max_chars=4000)
        if st.form_submit_button("Add entry"):
            try:
                store.note(case_id, kind, author, note)
                st.rerun()
            except CaseError as exc:
                st.error(str(exc))

    with st.expander("Update status or owner"):
        with st.form(f"update_{case_id}_{version}"):
            status = st.selectbox("New status", STATES, index=STATES.index(case["status"]))
            owner = st.text_input("Case owner", case["owner"] or "", max_chars=100)
            resolution = st.text_area("Resolution note (required to close)", max_chars=4000)
            author = st.text_input("Changed by", max_chars=100)
            if st.form_submit_button("Update case"):
                try:
                    store.change(case_id, status, owner, resolution, author)
                    st.rerun()
                except CaseError as exc:
                    st.error(str(exc))


def handoff(case):
    case_id = case["id"]
    with st.container(key="handoff_panel"):
        st.subheader("Analyst handoff")
        st.caption("Deterministic briefing from the saved case, linked records, and attributed analyst entries.")
        revisions = store.revisions(case_id)
        if revisions:
            if revisions[0]["case_version"] != case["version"]:
                st.warning("Latest handoff predates the current case. Review and save a new revision.")
            records = {row["revision"]: row for row in revisions}
            selected = st.selectbox("Saved revision", list(records), format_func=lambda number: f"Revision {number}")
            row = records[selected]
            if row["case_version"] != case["version"]:
                st.caption("This saved revision predates the latest case update.")
            st.code(row["markdown"], language=None, wrap_lines=True)
            st.download_button("Download handoff Markdown", row["markdown"],
                               f"handoff-{case_id[:8]}-r{selected}.md", "text/markdown", on_click="ignore")
        else:
            st.info("No handoff has been saved for this case.")

        with st.form(f"preview_{case_id}"):
            next_owner = st.text_input("Next owner", max_chars=100)
            action = st.text_area("Next action", max_chars=1000)
            if st.form_submit_button("Review handoff"):
                try:
                    version, preview = store.preview(case_id, next_owner, action)
                    st.session_state.preview = (case_id, version, next_owner, action, preview)
                except CaseError as exc:
                    st.session_state.pop("preview", None)
                    st.error(str(exc))
        pending = st.session_state.get("preview")
        if pending and pending[0] == case_id:
            _, version, owner, action, preview = pending
            st.subheader("Review draft briefing")
            st.code(preview, language=None, wrap_lines=True)
            if st.button("Save reviewed handoff", type="primary"):
                try:
                    store.finalize(case_id, version, owner, action, preview)
                    del st.session_state.preview
                    st.rerun()
                except CaseError as exc:
                    st.error(str(exc))


with st.container(key="hero"):
    st.title("CyberOps AI")
    st.write("Link alert evidence, track the case, and hand off a reviewed briefing.")
    st.caption("Local SOC case management · Maintained by Prashant Chauhan")

left, right = st.columns(2)
with left:
    st.subheader("Import alerts")
    one, two = st.columns(2)
    if one.button("Load identity demo", type="primary"):
        intake((ROOT / "samples/identity.json").read_bytes())
    if two.button("Load endpoint demo"):
        intake((ROOT / "samples/endpoint.json").read_bytes())
    with st.form("import"):
        upload = st.file_uploader("Alert JSON (up to 1 MiB)", type=["json"])
        if st.form_submit_button("Import JSON"):
            if upload:
                intake(upload.getvalue())
            else:
                st.error("Choose a JSON file.")
    if result := st.session_state.get("import_result"):
        counts, issues = result
        st.success(" · ".join(f"{key.title()}: {value}" for key, value in counts.items()))
        for issue in issues:
            st.text(issue)

with right:
    st.subheader("Case queue")
    with st.form("create"):
        title = st.text_input("Case title", max_chars=160)
        severity = st.selectbox("Case severity", SEVERITIES, index=2)
        owner = st.text_input("Initial owner", max_chars=100)
        if st.form_submit_button("Create case", type="primary"):
            try:
                st.session_state.case_picker = store.create(title, severity, owner)
            except CaseError as exc:
                st.error(str(exc))
    options = {row["id"]: f"{row['title']} · {row['status']} · {row['id'][:8]}" for row in store.list_cases()}
    selected = None
    if options:
        if st.session_state.get("case_picker") not in options:
            st.session_state.case_picker = next(iter(options))
        selected = st.selectbox("Select case", list(options), index=None, format_func=options.__getitem__,
                                key="case_picker")
    else:
        st.info("Create a case to start reviewing alerts.")

if selected:
    case, linked, events = store.detail(selected)
    with st.container(key="case_panel"):
        st.subheader("Case detail")
        st.text(f"{case['title']} · {case['severity']} · {case['status']} · Owner {case['owner'] or 'Unknown'}")
        section = st.radio("Case workspace", ["Timeline and evidence", "Handoff"], horizontal=True)
        if section == "Timeline and evidence":
            overview(case, linked, events)
        else:
            handoff(case)
