"""
Post-Production Intake Agent — core pipeline (v2: direct Shade API).

Given one or more Shade folder paths, this:
1. Lists video files in those folders via Shade's search/files endpoint.
2. Pulls precise, speaker-labeled transcripts via Shade's utterances endpoint.
3. Tags flagged moments (topic, emotional weight, off-record/legal flags)
   with Claude, inferring the subject's name from the transcript itself.
4. Writes an Interview + Selects rows into Notion.
5. Generates a combined Google Doc (selects + full transcript) per interview.
"""

import io
import json
import re
from datetime import datetime, timezone

import anthropic
import requests
from googleapiclient.http import MediaIoBaseUpload
from notion_client import Client as NotionClient

# --- Fixed workspace IDs ---------------------------------------------------

INTERVIEWS_DB_ID = "9f722960ac654e19abe921a6a8480a24"
INTERVIEWS_DATA_SOURCE_ID = "31cf1c34-9079-4f9b-814b-cefdc8b9bcd9"
SELECTS_DB_ID = "08ed992eddbb496ca2557de69f36de0f"
SELECTS_DB_URL = "https://app.notion.com/p/08ed992eddbb496ca2557de69f36de0f"
COMPLETED_DOCS_FOLDER_ID = "11fqzrLXi5iwur_eOvSv8kMK6P3bAiEgs"

SHADE_DRIVE_ID = "4ac63729-7c15-4a5b-b954-4edfd4700643"  # Buoyant workspace — fixed
SHADE_API_BASE = "https://api.shade.inc"

CLAUDE_MODEL = "claude-sonnet-5"  # adjust if your API access differs

SYSTEM_PROMPT = """You are tagging a documentary interview transcript for post-production.

You will receive a JSON array of utterances from a Shade transcript export.
Each utterance has: speaker (a letter like "A", "B", "C"), start and end
(milliseconds), and text.

First, work out which speaker is the INTERVIEW SUBJECT (not the
interviewer/crew) and what their name is — subjects almost always
introduce themselves early on ("My name is..."). There may be more than
one subject in a panel-style interview.

Then produce a list of flagged, editor-ready selects drawn ONLY from the
subject's utterances (never the interviewer's lines). For each:

- Combine consecutive utterances as needed so the quote is a complete,
  standalone thought — no dangling pronouns ("it", "that", "this")
  pointing at something outside the quote. Someone who has never seen the
  transcript should fully understand it.
- Use the earliest start (ms) and latest end (ms) among the utterances
  you combined for that quote.
- Assign one or more Topic tags describing the subject matter (e.g.
  "Origin story", "Historical context", "Legacy", "Personal reflection",
  "Voting rights", "Mission", "Achievement", "Early fundraising",
  "Personal anecdote" — introduce a new concise tag if none fit).
- Assign exactly one Emotional Weight: "Powerful", "Reflective",
  "Light/Humor", "Tense", "Warm/Inspiring", or "Neutral".
- Assign a Length Type: "Soundbite" (roughly 8-25 seconds) or "Long-form"
  (genuinely needs more time for a complete thought).
- Set Flag to "Off the Record" or "Legal-Sensitive" whenever the
  interviewer or subject explicitly signals something is off-camera/
  off-record (e.g. "cut the camera", "stop rolling"), or the content
  touches active litigation, legal disputes, or personal health/medical
  information not clearly meant for the documentary. Otherwise use "None".
  Flagged moments are NOT normal selects — keep the quote minimal (a
  description is fine) and never omit a flag to make a moment look usable.
- Skip filler, false starts, and purely administrative dialogue.

Return ONLY valid JSON — no prose, no markdown code fences — as an object:
{
  "subject_name": "...",
  "selects": [
    {"start_ms": 0, "end_ms": 0, "quote": "...", "topic_tags": ["..."],
     "emotional_weight": "...", "length_type": "...", "flag": "None"}
  ]
}
"""


# --- Shade API -------------------------------------------------------------

def shade_headers(api_key):
    return {"Authorization": api_key, "accept": "application/json"}


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def drive_id_from_path(folder_path):
    """Shade paths start with the drive ID (/<drive_id>/...). Use it when
    present so folders in other drives (e.g. Collab) work, and fall back to
    the main Buoyant drive otherwise."""
    first = folder_path.strip("/").split("/", 1)[0]
    return first if UUID_RE.match(first) else SHADE_DRIVE_ID


def shade_list_files(api_key, drive_id, folder_path, recursive=True):
    resp = requests.post(
        f"{SHADE_API_BASE}/search/files",
        headers={**shade_headers(api_key), "content-type": "application/json"},
        json={
            "path": folder_path,
            "drive_id": drive_id,
            "recursive": recursive,
            "query": "",
            "page": 0,
            "limit": 200,
            "filters": [],
        },
        timeout=30,
    )
    resp.raise_for_status()
    files = resp.json()
    return [f for f in files if f.get("type") == "VIDEO"]


def shade_get_utterances(api_key, drive_id, asset_id):
    resp = requests.get(
        f"{SHADE_API_BASE}/assets/{asset_id}/transcription/utterances",
        headers=shade_headers(api_key),
        params={"drive_id": drive_id},
        timeout=30,
    )
    resp.raise_for_status()
    return normalize_utterances(resp.json())


def normalize_utterances(data):
    """Shade has returned transcripts both as a bare list of utterances and
    wrapped in an object (and sometimes as a JSON string). Accept all of
    these and hand back a plain list of utterance dicts."""
    if isinstance(data, str):
        data = data.strip()
        if not data:
            return []
        data = json.loads(data)
    if isinstance(data, dict):
        for key in ("utterances", "items", "data", "results", "transcript", "transcription"):
            if key in data:
                return normalize_utterances(data[key])
        lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if lists:
            return normalize_utterances(lists[0])
        raise ValueError(f"Unrecognized transcript format from Shade (keys: {', '.join(data.keys())})")
    if not data:
        return []
    out = []
    for u in data:
        if not isinstance(u, dict) or not str(u.get("text", "")).strip():
            continue
        words = u.get("words")
        if not (isinstance(words, list) and all(isinstance(w, dict) and "text" in w and "start" in w for w in words)):
            words = []
        out.append({
            "speaker": u.get("speaker", "?"),
            "start": u.get("start", 0),
            "end": u.get("end", u.get("start", 0)),
            "text": u["text"],
            "words": words,
        })
    return out


# --- Helpers ----------------------------------------------------------------

def ms_to_timecode(ms):
    total_seconds = int(ms / 1000)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def tag_utterances(claude, utterances):
    full_text = ""
    with claude.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(utterances)}],
        extra_body={"output_config": {"effort": "low"}},
    ) as stream:
        for chunk in stream.text_stream:
            full_text += chunk
        stop_reason = stream.get_final_message().stop_reason

    if not full_text.strip():
        raise ValueError(
            f"Claude returned no text content (stop_reason={stop_reason}). "
            "This usually means the response was cut off before finishing — "
            "try raising max_tokens further, or splitting very long interviews."
        )
    raw = re.sub(r"^```(json)?|```$", "", full_text.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)


# --- Notion writes -----------------------------------------------------------

def find_existing_interviews(notion, asset_id):
    """Return any Interview rows already created for this Shade asset."""
    resp = notion.data_sources.query(
        data_source_id=INTERVIEWS_DATA_SOURCE_ID,
        filter={"property": "Shade Asset ID", "rich_text": {"equals": asset_id}},
    )
    return resp.get("results", [])


def is_complete(page):
    """An interview counts as done only once its Google Doc link was saved."""
    return bool(page.get("properties", {}).get("Transcript Link", {}).get("url"))


def archive_incomplete(notion, page):
    """Remove a half-finished Interview row (and its Selects) left by a run
    that crashed before the doc was made, so the clip can be redone cleanly."""
    for rel in page.get("properties", {}).get("Selects", {}).get("relation", []):
        notion.pages.update(page_id=rel["id"], archived=True)
    notion.pages.update(page_id=page["id"], archived=True)


def create_interview_page(notion, subject, project, asset_id, shade_source_link=None):
    props = {
        "Subject / Speaker": {"title": [{"text": {"content": subject}}]},
        "Project": {"select": {"name": project}},
        "Date Generated": {"date": {"start": datetime.now(timezone.utc).date().isoformat()}},
        "Status": {"select": {"name": "Tagged"}},
        "Shade Asset ID": {"rich_text": [{"text": {"content": asset_id}}]},
    }
    if shade_source_link:
        props["Shade Source Link"] = {"url": shade_source_link}
    page = notion.pages.create(parent={"database_id": INTERVIEWS_DB_ID}, properties=props)
    return page["id"]


def create_select_rows(notion, interview_page_id, selects):
    for s in selects:
        notion.pages.create(
            parent={"database_id": SELECTS_DB_ID},
            properties={
                "Quote": {"title": [{"text": {"content": s["quote"][:200]}}]},
                "Interview": {"relation": [{"id": interview_page_id}]},
                "Timecode Start": {"rich_text": [{"text": {"content": ms_to_timecode(s["start_ms"])}}]},
                "Timecode End": {"rich_text": [{"text": {"content": ms_to_timecode(s["end_ms"])}}]},
                "Topic Tags": {"multi_select": [{"name": t} for t in s.get("topic_tags", [])]},
                "Emotional Weight": {"select": {"name": s["emotional_weight"]}},
                "Length Type": {"select": {"name": s["length_type"]}},
                "Flag": {"select": {"name": s.get("flag") or "None"}},
                "Review Status": {"select": {"name": "Flagged"}},
            },
        )


def update_transcript_link(notion, interview_page_id, doc_url):
    notion.pages.update(page_id=interview_page_id, properties={"Transcript Link": {"url": doc_url}})


# --- Google Doc generation ---------------------------------------------------

def format_utterance(u, sentences_per_marker=3):
    """Render one utterance with a timecode every N sentences (using
    word-level timestamps) — frequent enough that long speaker turns
    aren't left with only one reference point, but not so frequent that
    it clutters the read."""
    words = u.get("words") or []
    if not words:
        return f"<p><b>Speaker {u['speaker']}</b><br/>[{ms_to_timecode(u['start'])}] {u['text']}</p>"

    sentences = []
    current_words = []
    current_start = words[0]["start"]
    for w in words:
        if not current_words:
            current_start = w["start"]
        current_words.append(w["text"])
        if w["text"].rstrip().endswith((".", "?", "!")):
            sentences.append((current_start, " ".join(current_words)))
            current_words = []
    if current_words:
        sentences.append((current_start, " ".join(current_words)))

    chunks = []
    for i in range(0, len(sentences), sentences_per_marker):
        group = sentences[i:i + sentences_per_marker]
        chunks.append((group[0][0], " ".join(s[1] for s in group)))

    body = " ".join(f"[{ms_to_timecode(start)}] {text}" for start, text in chunks)
    return f"<p><b>Speaker {u['speaker']}</b><br/>{body}</p>"


def build_doc_html(subject, selects, utterances, source_filename=None):
    rows = []
    for s in selects:
        if s.get("flag") and s["flag"] != "None":
            continue
        length_note = " <i>(long-form)</i>" if s.get("length_type") == "Long-form" else ""
        rows.append(
            f"<tr><td>{ms_to_timecode(s['start_ms'])}\u2013{ms_to_timecode(s['end_ms'])}</td>"
            f"<td>\"{s['quote']}\"{length_note}</td>"
            f"<td>{', '.join(s.get('topic_tags', []))}</td>"
            f"<td>{s['emotional_weight']}</td></tr>"
        )

    flagged = [s for s in selects if s.get("flag") and s["flag"] != "None"]
    flagged_html = ""
    if flagged:
        items = "".join(
            f"<li>{ms_to_timecode(s['start_ms'])}\u2013{ms_to_timecode(s['end_ms'])}: {s['quote']} "
            f"(<b>{s['flag']}</b>)</li>"
            for s in flagged
        )
        flagged_html = f"""
        <h3>\u26a0 Flagged \u2014 Not for General Use</h3>
        <ul>{items}</ul>
        <p>Excluded from the table above on purpose \u2014 consult producer/legal
        before referencing this footage.</p>
        """

    transcript_rows = "".join(format_utterance(u) for u in utterances)

    source_line = f"<p><i>Source file: {source_filename}</i></p>" if source_filename else ""

    return f"""<html><body>
<h1>{subject}</h1>
{source_line}
<p><a href="{SELECTS_DB_URL}">\u2192 Browse all tagged soundbites across every interview in Notion</a></p>
<p><i>Selects below are generated automatically. Full transcript follows for reference \u2014 scroll down.</i></p>
<h2>Selects</h2>
<table border="1" cellspacing="0" cellpadding="6">
<tr><th>Timecode</th><th>Quote</th><th>Topic</th><th>Emotional Weight</th></tr>
{''.join(rows)}
</table>
{flagged_html}
<hr/>
<h2>Full Transcript</h2>
{transcript_rows}
</body></html>"""


def create_combined_doc(drive, subject, project, html_content):
    file_metadata = {
        "name": f"{project} \u2014 {subject} (Selects + Transcript)",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [COMPLETED_DOCS_FOLDER_ID],
    }
    media = MediaIoBaseUpload(io.BytesIO(html_content.encode("utf-8")), mimetype="text/html")
    doc = drive.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink", supportsAllDrives=True
    ).execute()
    return doc["webViewLink"]


# --- Orchestration ------------------------------------------------------------

def process_folder(shade_api_key, drive, notion, claude, project, folder_path, recursive, log):
    drive_id = drive_id_from_path(folder_path)
    videos = shade_list_files(shade_api_key, drive_id, folder_path, recursive=recursive)
    log(f"  Found {len(videos)} video(s) in drive {drive_id}")
    results = []
    for video in videos:
        asset_id = video["id"]
        name = video.get("name", asset_id)
        try:
            existing = find_existing_interviews(notion, asset_id)
            if any(is_complete(pg) for pg in existing):
                log(f"  \u23ed {name}: already processed previously, skipping")
                continue
            for pg in existing:
                archive_incomplete(notion, pg)
            if existing:
                log(f"  \u267b {name}: clearing an unfinished earlier attempt")

            log(f"Fetching transcript for {name}...")
            utterances = shade_get_utterances(shade_api_key, drive_id, asset_id)
            if not utterances:
                log(f"  \u23ed {name}: no transcript available, skipping")
                continue

            tagged = tag_utterances(claude, utterances)
            subject = tagged.get("subject_name") or name
            selects = tagged["selects"]

            html = build_doc_html(subject, selects, utterances, source_filename=name)

            interview_id = create_interview_page(notion, subject, project, asset_id)
            create_select_rows(notion, interview_id, selects)

            doc_url = create_combined_doc(drive, subject, project, html)
            update_transcript_link(notion, interview_id, doc_url)

            results.append({
                "subject": subject, "project": project, "asset_name": name,
                "selects_count": len(selects), "doc_url": doc_url, "status": "ok",
            })
            log(f"  \u2705 {subject}: {len(selects)} selects tagged")
        except Exception as e:
            results.append({"subject": name, "project": project, "status": "error", "error": str(e)})
            log(f"  \u274c {name} failed: {e}")
    return results
