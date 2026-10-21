# Post-Production Intake Agent

Pulls transcripts directly from Shade's API for a given folder (or
folders), tags flagged moments (topic, emotional weight, off-record/legal
flags), adds each select as a timed comment on the clip in Shade, and
generates one combined Google Doc per interview: a link to the clip, the
selects table (each timecode links to its comment in Shade), and the full
transcript.

## One-time setup

### 1. Shade API key
Generate an API key in Shade's account/workspace settings (Admin →
API Keys, or similar). This is `shade_api_key`. Confirmed working format:
sent as the raw key in the `Authorization` header (no `Bearer` prefix).

### 2. Google service account (for writing the output Doc)
1. In Google Cloud Console, enable the **Google Drive API** for a
   project.
2. Create a **Service Account**, then create a JSON key for it — this
   goes into `secrets.toml` under `[gcp_service_account]`.
3. Share the **Post-Production — Completed Docs** Drive folder with the
   service account's email (`client_email` in the JSON key), giving it
   **Editor** access.

### 3. Anthropic API key
Create one at console.anthropic.com if you don't already have one for
this — this is `anthropic_api_key`.

### 4. Local secrets (for testing before deploying)
Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and
fill in the three values above. This file is gitignored — never commit it.

## Running locally
```
pip install -r requirements.txt
streamlit run app.py
```

## Deploying to Streamlit Community Cloud
1. Push this repo to GitHub.
2. On share.streamlit.io, create a new app pointing at the repo, main
   file `app.py`.
3. In the app's **Settings → Secrets**, paste in the same three values
   from your local `secrets.toml`.
4. Deploy.

## Using it
1. Pick the project from the dropdown.
2. Paste the Shade folder link(s), copied from the browser's address bar
   while viewing the folder, for folders that actually contain interview
   footage (not B-roll or stills). Any drive works. One link per line.
3. Hit **Run Intake**. Every video transcript in those folders gets
   pulled from Shade, tagged, marked with comments in Shade, and turned
   into a combined doc. Clips that already have a doc are skipped, so a
   folder can safely be re-run after new footage is added.

The subject's name is inferred automatically from the transcript itself
(documentary subjects almost always introduce themselves early on) —
double check it in the doc in case an unusual interview trips this up.

Flagged moments (off the record, legal-sensitive) are listed separately in
the doc and are never posted as comments in Shade.

## Known open items
- Docs made before the switch away from Notion aren't tagged with their
  Shade clip ID, so re-running a folder processed before then makes new
  docs for those clips.
