import re
from urllib.parse import unquote, urlparse

import streamlit as st
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

from pipeline import process_folder

PROJECTS = ["CBCF", "Yaida Ford Campaign", "DC Advocacy", "ISTELive", "Other"]

st.set_page_config(page_title="Post-Production Intake Agent", page_icon="🎬")
st.title("🎬 Post-Production Intake Agent")
st.caption(
    "Pulls transcripts directly from Shade, tags selects, marks each one "
    "with a comment on the clip in Shade, and generates a combined selects + "
    "transcript doc per interview."
)

with st.expander("How this works", expanded=False):
    st.markdown(
        "1. Pick the project these interviews belong to.\n"
        "2. Paste one or more Shade folder links (copied from the browser's "
        "address bar while viewing the folder) or Shade folder paths — the ones that actually "
        "contain interview footage (not B-roll/stills). One per line. You "
        "can find a folder's path in the `search/files` request in your "
        "browser's dev tools while browsing it in Shade.\n"
        "3. Hit **Run Intake**. Every video in those folders gets its "
        "transcript pulled from Shade and tagged. Each select is added as a "
        "comment on the clip in Shade, and everything lands in a combined "
        "Google Doc in **Post-Production — Completed Docs**, with a link to "
        "the clip and each select's timecode linked to its spot in Shade. "
        "Clips that already have a doc are skipped."
    )

project = st.selectbox("Project", PROJECTS)
folder_paths_raw = st.text_area(
    "Shade folder path(s) — one per line",
    placeholder="/4ac63729-7c15-4a5b-b954-4edfd4700643/All Assets/02 FOOTAGE/Interviews",
    height=100,
)
recursive = st.checkbox("Include subfolders", value=True)


def parse_source(raw):
    """Repeatedly decode URL-encoding until stable, so pasted paths work
    whether they're plain, single-encoded, or double-encoded (e.g. a
    browser-copied path that got encoded again by another app)."""
    path = raw.strip()
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    # Strip invisible characters (zero-width spaces etc.) picked up from copy/paste.
    path = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", path).strip()
    # Accept a Shade folder link from the browser address bar:
    # https://app.shade.inc/<workspace>/f/<drive label>/<drive id>/<folder path>
    # The workspace and drive label are what Shade uses in links to clips.
    workspace = drive_label = None
    if path.startswith(("http://", "https://")):
        parts = [s for s in urlparse(path).path.split("/") if s]
        if "f" in parts:
            i = parts.index("f")
            workspace = parts[i - 1] if i > 0 else None
            drive_label = parts[i + 1] if len(parts) > i + 1 else None
            rest = parts[i + 2:]  # skip "f" and the drive label
            path = "/" + "/".join(s.strip() for s in rest)
    return path, workspace, drive_label


@st.cache_resource
def get_clients():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    drive = build("drive", "v3", credentials=creds)
    claude = anthropic.Anthropic(api_key=st.secrets["anthropic_api_key"])
    shade_api_key = st.secrets["shade_api_key"]
    return drive, claude, shade_api_key


if st.button("▶ Run Intake", type="primary"):
    sources = [parse_source(p) for p in folder_paths_raw.splitlines() if p.strip()]
    if not sources:
        st.warning("Paste at least one Shade folder path first.")
        st.stop()

    drive, claude, shade_api_key = get_clients()
    log_area = st.empty()
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        log_area.text("\n".join(log_lines))

    all_results = []
    with st.spinner("Processing..."):
        for path, workspace, drive_label in sources:
            log(f"Scanning folder: {path}")
            all_results.extend(
                process_folder(
                    shade_api_key, drive, claude, project, path, recursive, log,
                    workspace=workspace, drive_label=drive_label,
                )
            )

    if not all_results:
        st.info("Nothing new to process: no videos with transcripts, or every clip already has a doc.")
    else:
        ok = [r for r in all_results if r["status"] == "ok"]
        failed = [r for r in all_results if r["status"] == "error"]

        if ok:
            st.success(
                f"✅ Processed {len(ok)} interview(s), "
                f"{sum(r['selects_count'] for r in ok)} selects tagged."
            )
        for r in ok:
            st.markdown(f"- **{r['subject']}** ({r['project']}) — {r['selects_count']} selects — [Open doc]({r['doc_url']})")

        if failed:
            st.error(f"{len(failed)} item(s) failed — see details below.")
            for r in failed:
                st.markdown(f"- **{r['subject']}**: {r['error']}")
