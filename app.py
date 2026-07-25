import streamlit as st
import anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build
from notion_client import Client as NotionClient

from pipeline import process_folder

PROJECTS = ["CBCF", "Yaida Ford Campaign", "DC Advocacy", "ISTELive", "Other"]

st.set_page_config(page_title="Post-Production Intake Agent", page_icon="🎬")
st.title("🎬 Post-Production Intake Agent")
st.caption(
    "Pulls transcripts directly from Shade, tags selects, writes them to "
    "Notion, and generates a combined selects + transcript doc per interview."
)

with st.expander("How this works", expanded=False):
    st.markdown(
        "1. Pick the project these interviews belong to.\n"
        "2. Paste one or more Shade folder paths — the ones that actually "
        "contain interview footage (not B-roll/stills). One per line. You "
        "can find a folder's path in the `search/files` request in your "
        "browser's dev tools while browsing it in Shade.\n"
        "3. Hit **Run Intake**. Every video in those folders gets its "
        "transcript pulled from Shade, tagged, written to Notion, and "
        "turned into a combined Google Doc in **Post-Production — "
        "Completed Docs**."
    )

project = st.selectbox("Project", PROJECTS)
folder_paths_raw = st.text_area(
    "Shade folder path(s) — one per line",
    placeholder="/4ac63729-7c15-4a5b-b954-4edfd4700643/All Assets/02 FOOTAGE/Interviews",
    height=100,
)
recursive = st.checkbox("Include subfolders", value=True)


@st.cache_resource
def get_clients():
    creds = service_account.Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    drive = build("drive", "v3", credentials=creds)
    notion = NotionClient(auth=st.secrets["notion_token"])
    claude = anthropic.Anthropic(api_key=st.secrets["anthropic_api_key"])
    shade_api_key = st.secrets["shade_api_key"]
    return drive, notion, claude, shade_api_key


if st.button("▶ Run Intake", type="primary"):
    folder_paths = [p.strip() for p in folder_paths_raw.splitlines() if p.strip()]
    if not folder_paths:
        st.warning("Paste at least one Shade folder path first.")
        st.stop()

    drive, notion, claude, shade_api_key = get_clients()
    log_area = st.empty()
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        log_area.text("\n".join(log_lines))

    all_results = []
    with st.spinner("Processing..."):
        for path in folder_paths:
            log(f"Scanning folder: {path}")
            all_results.extend(
                process_folder(shade_api_key, drive, notion, claude, project, path, recursive, log)
            )

    if not all_results:
        st.info("No videos with transcripts found in the folder(s) given.")
    else:
        ok = [r for r in all_results if r["status"] == "ok"]
        failed = [r for r in all_results if r["status"] == "error"]

        st.success(
            f"✅ Notion updated — processed {len(ok)} interview(s), "
            f"{sum(r['selects_count'] for r in ok)} selects tagged."
        )
        for r in ok:
            st.markdown(f"- **{r['subject']}** ({r['project']}) — {r['selects_count']} selects — [Open doc]({r['doc_url']})")

        if failed:
            st.error(f"{len(failed)} item(s) failed — see details below.")
            for r in failed:
                st.markdown(f"- **{r['subject']}**: {r['error']}")
