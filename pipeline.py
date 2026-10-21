"""
Post-Production Intake Agent — core pipeline (v2: direct Shade API).

Given one or more Shade folder paths, this:
1. Lists video files in those folders via Shade's search/files endpoint.
2. Pulls precise, speaker-labeled transcripts via Shade's utterances endpoint.
3. Tags flagged moments (topic, emotional weight, off-record/legal flags)
   with Claude, inferring the subject's name from the transcript itself.
4. Posts a ranged comment on the clip in Shade for each select.
5. Generates a combined Google Doc (clip link, selects linked to their
   Shade comments, full transcript) per interview.
"""

import io
import json
import re

import anthropic
import requests
from googleapiclient.http import MediaIoBaseUpload

# --- Fixed workspace IDs ---------------------------------------------------

COMPLETED_DOCS_FOLDER_ID = "11fqzrLXi5iwur_eOvSv8kMK6P3bAiEgs"

SHADE_DRIVE_ID = "4ac63729-7c15-4a5b-b954-4edfd4700643"  # Buoyant workspace — fixed
SHADE_API_BASE = "https://api.shade.inc"
SHADE_WEB_BASE = "https://app.shade.inc"
DEFAULT_WORKSPACE = "buoyant"  # the workspace's web address; unchanged by the display-name rename

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


def apply_transcript_edits(base, edits):
    """Swap in any corrections someone made to the transcript in Shade.
    Edits are keyed by the utterance's position in the list."""
    if not isinstance(edits, dict) or not edits:
        return base
    merged = list(base)
    for key, edit in edits.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(merged) and isinstance(merged[i], dict):
            if isinstance(edit, dict):
                merged[i] = {**merged[i], **edit}
            elif isinstance(edit, str):
                merged[i] = {**merged[i], "text": edit, "words": []}
    return merged


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
        # Shade's documented shape: {"base_utterances": [...], "edits": {...}}
        if isinstance(data.get("base_utterances"), list):
            return normalize_utterances(apply_transcript_edits(data["base_utterances"], data.get("edits")))
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


# --- Shade links and comments ------------------------------------------------

_LINK_INFO_CACHE = {}


def shade_drive_link_info(api_key, drive_id):
    """Look up the workspace web name and drive label Shade uses in its web
    links (e.g. "buoyant" and "COL"). Only needed when a plain folder path
    was pasted instead of a browser link. Returns (None, None) if unknown."""
    if drive_id in _LINK_INFO_CACHE:
        return _LINK_INFO_CACHE[drive_id]
    found = (None, None)
    try:
        ws_resp = requests.get(f"{SHADE_API_BASE}/workspaces", headers=shade_headers(api_key), timeout=30)
        ws_resp.raise_for_status()
        workspaces = ws_resp.json()
        if isinstance(workspaces, dict):
            workspaces = workspaces.get("workspaces") or workspaces.get("items") or []
        for ws in workspaces:
            d_resp = requests.get(
                f"{SHADE_API_BASE}/workspaces/{ws['id']}/drives", headers=shade_headers(api_key), timeout=30
            )
            if not d_resp.ok:
                continue
            drives = d_resp.json()
            if isinstance(drives, dict):
                drives = drives.get("drives") or drives.get("items") or []
            for d in drives:
                if d.get("id") == drive_id and d.get("identifier"):
                    found = (ws.get("domain") or DEFAULT_WORKSPACE, d["identifier"])
                    break
            if found[1]:
                break
    except Exception:
        pass
    _LINK_INFO_CACHE[drive_id] = found
    return found


def shade_clip_url(workspace, drive_label, asset_id):
    if not (workspace and drive_label):
        return None
    return f"{SHADE_WEB_BASE}/{workspace}/a/{drive_label}/{asset_id}"


def comment_text(s):
    tags = ", ".join(s.get("topic_tags", []))
    meta = " · ".join(x for x in (tags, s.get("emotional_weight")) if x)
    quote = s["quote"] if len(s["quote"]) <= 600 else s["quote"][:597] + "..."
    return f"SELECT: \"{quote}\"" + (f"\n{meta}" if meta else "")


def shade_existing_comments(api_key, drive_id, asset_id):
    """Map comment text -> comment ID for comments already on the clip, so a
    re-run doesn't post the same select twice."""
    resp = requests.get(
        f"{SHADE_API_BASE}/assets/{asset_id}/comments",
        headers=shade_headers(api_key),
        params={"drive_id": drive_id},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("comments") or data.get("items") or []
    out = {}
    for c in data if isinstance(data, list) else []:
        if isinstance(c, dict):
            text = c.get("comment") or c.get("text") or ""
            if text:
                out[text.strip()] = c.get("id")
    return out


def shade_add_comment(api_key, drive_id, asset_id, clip_url, text, start_ms, end_ms):
    """Post a public comment covering the select's time range. Returns the
    new comment's ID when Shade sends one back."""
    body = {
        "drive_id": drive_id,
        "comment": text,
        "timestamp": round(start_ms / 1000, 2),
        "duration": round(max(end_ms - start_ms, 1000) / 1000, 2),
        "url": f"{clip_url}?commentId=REPLACE_COMMENT_ID_ON_SERVER",
        "is_public": True,
    }
    resp = requests.post(
        f"{SHADE_API_BASE}/assets/{asset_id}/comments",
        headers={**shade_headers(api_key), "content-type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        if data.get("id"):
            return data["id"]
        if isinstance(data.get("comment"), dict):
            return data["comment"].get("id")
    return None


def mark_selects_in_shade(api_key, drive_id, asset_id, clip_url, selects, log, name):
    """Add a Shade comment per usable select and attach a link to it on each
    select (falls back to the clip link). Flagged moments are never posted."""
    usable = [s for s in selects if not (s.get("flag") and s["flag"] != "None")]
    try:
        existing = shade_existing_comments(api_key, drive_id, asset_id)
    except Exception as e:
        log(f"  \u26a0 {name}: couldn't read existing Shade comments ({e}); skipping comments")
        return
    posted = 0
    for s in usable:
        text = comment_text(s)
        try:
            cid = existing.get(text.strip())
            if not cid and text.strip() not in existing:
                cid = shade_add_comment(api_key, drive_id, asset_id, clip_url, text, s["start_ms"], s["end_ms"])
                posted += 1
            s["link"] = f"{clip_url}?commentId={cid}" if cid else clip_url
        except Exception as e:
            log(f"  \u26a0 {name}: couldn't add a Shade comment at {ms_to_timecode(s['start_ms'])} ({e})")
            s["link"] = clip_url
    log(f"  \U0001f4ac {name}: {posted} new comment(s) added in Shade")


# --- Google Drive duplicate check ---------------------------------------------

def find_existing_doc(drive, asset_id):
    """Return the link to a doc this app already made for the clip, if any."""
    q = (
        f"appProperties has {{ key='shadeAssetId' and value='{asset_id}' }} "
        f"and trashed = false"
    )
    resp = drive.files().list(
        q=q, fields="files(id, webViewLink)", supportsAllDrives=True, includeItemsFromAllDrives=True, pageSize=1
    ).execute()
    files = resp.get("files", [])
    return files[0]["webViewLink"] if files else None


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


def build_doc_html(subject, selects, utterances, source_filename=None, clip_url=None):
    rows = []
    for s in selects:
        if s.get("flag") and s["flag"] != "None":
            continue
        length_note = " <i>(long-form)</i>" if s.get("length_type") == "Long-form" else ""
        tc = f"{ms_to_timecode(s['start_ms'])}\u2013{ms_to_timecode(s['end_ms'])}"
        if s.get("link"):
            tc = f'<a href="{s["link"]}">{tc}</a>'
        rows.append(
            f"<tr><td>{tc}</td>"
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
    clip_line = (
        f'<p><a href="{clip_url}">\u25b6 Watch the source clip in Shade</a></p>' if clip_url else ""
    )
    tc_note = " Click a timecode to jump to that moment in Shade." if clip_url else ""

    return f"""<html><body>
<h1>{subject}</h1>
{source_line}
{clip_line}
<p><i>Selects below are generated automatically.{tc_note} Full transcript follows for reference \u2014 scroll down.</i></p>
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


def create_combined_doc(drive, subject, project, html_content, asset_id):
    file_metadata = {
        "name": f"{project} \u2014 {subject} (Selects + Transcript)",
        "mimeType": "application/vnd.google-apps.document",
        "parents": [COMPLETED_DOCS_FOLDER_ID],
        "appProperties": {"shadeAssetId": asset_id},
    }
    media = MediaIoBaseUpload(io.BytesIO(html_content.encode("utf-8")), mimetype="text/html")
    doc = drive.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink", supportsAllDrives=True
    ).execute()
    return doc["webViewLink"]


# --- Orchestration ------------------------------------------------------------

def process_folder(shade_api_key, drive, claude, project, folder_path, recursive, log,
                   workspace=None, drive_label=None):
    drive_id = drive_id_from_path(folder_path)
    if not drive_label:
        workspace, drive_label = shade_drive_link_info(shade_api_key, drive_id)
    workspace = workspace or DEFAULT_WORKSPACE
    if not drive_label:
        log("  \u26a0 Couldn't work out this drive's Shade web link, so docs won't link to clips and "
            "no comments will be added. Paste the folder's browser link instead of a path to fix this.")

    videos = shade_list_files(shade_api_key, drive_id, folder_path, recursive=recursive)
    log(f"  Found {len(videos)} video(s) in drive {drive_id}")
    results = []
    for video in videos:
        asset_id = video["id"]
        name = video.get("name", asset_id)
        try:
            if find_existing_doc(drive, asset_id):
                log(f"  \u23ed {name}: already has a doc, skipping")
                continue

            log(f"Fetching transcript for {name}...")
            utterances = shade_get_utterances(shade_api_key, drive_id, asset_id)
            if not utterances:
                log(f"  \u23ed {name}: no transcript available, skipping")
                continue

            tagged = tag_utterances(claude, utterances)
            subject = tagged.get("subject_name") or name
            selects = tagged.get("selects") or []

            clip_url = shade_clip_url(workspace, drive_label, asset_id)
            if clip_url:
                mark_selects_in_shade(shade_api_key, drive_id, asset_id, clip_url, selects, log, name)

            html = build_doc_html(subject, selects, utterances, source_filename=name, clip_url=clip_url)
            doc_url = create_combined_doc(drive, subject, project, html, asset_id)

            results.append({
                "subject": subject, "project": project, "asset_name": name,
                "selects_count": len(selects), "doc_url": doc_url, "status": "ok",
            })
            log(f"  \u2705 {subject}: {len(selects)} selects tagged")
        except Exception as e:
            results.append({"subject": name, "project": project, "status": "error", "error": str(e)})
            log(f"  \u274c {name} failed: {e}")
    return results
