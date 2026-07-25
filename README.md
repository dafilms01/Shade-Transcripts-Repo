# Post-Production Intake Agent

Pulls transcripts directly from Shade's API for a given folder (or
folders), tags flagged moments (topic, emotional weight, off-record/legal
flags), writes them into the Notion Post-Production databases, and
generates one combined Google Doc (selects + full transcript) per
interview.

## One-time setup

### 1. Shade API key
Generate an API key in Shade's account/workspace settings (Admin →
API Keys, or similar). This is `shade_api_key`. The app sends it as
`Authorization: Bearer <key>` — if Shade rejects that, check the code
sample Shade shows next to the key and adjust `shade_headers()` in
`pipeline.py` accordingly (it's a one-line change).

### 2. Notion integration
1. Go to notion.so/my-integrations → **New integration**. Name it
   something like "Post-Production Agent". Copy the token — this is
   `notion_token`.
2. Open the **CBCF-50 Short Film Project** page in Notion → `···` menu →
   **Connections** → add your new integration.

### 3. Google service account (for writing the output Doc)
1. In Google Cloud Console, enable the **Google Drive API** for a
   project.
2. Create a **Service Account**, then create a JSON key for it — this
   goes into `secrets.toml` under `[gcp_service_account]`.
3. Share the **Post-Production — Completed Docs** Drive folder with the
   service account's email (`client_email` in the JSON key), giving it
   **Editor** access.

### 4. Anthropic API key
Create one at console.anthropic.com if you don't already have one for
this — this is `anthropic_api_key`.

### 5. Local secrets (for testing before deploying)
Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and
fill in the four values above. This file is gitignored — never commit it.

## Running locally
```
pip install -r requirements.txt
streamlit run app.py
```

## Deploying to Streamlit Community Cloud
1. Push this repo to GitHub.
2. On share.streamlit.io, create a new app pointing at the repo, main
   file `app.py`.
3. In the app's **Settings → Secrets**, paste in the same four values
   from your local `secrets.toml`.
4. Deploy.

## Using it
1. Pick the project from the dropdown.
2. Paste in the Shade folder path(s) that actually contain interview
   footage — not B-roll or stills folders. You can grab a folder's exact
   path from the `search/files` request in your browser's dev tools while
   browsing that folder in Shade. One path per line if there's more than
   one (e.g. a project with interviews split across a few folders).
3. Hit **Run Intake**. Every video transcript in those folders gets
   pulled from Shade directly (no manual export needed), tagged, written
   to Notion, and turned into a combined doc.

The subject's name is inferred automatically from the transcript itself
(documentary subjects almost always introduce themselves early on) —
double check it in Notion afterward in case an unusual interview trips
this up.

## Known open items
- **Shade auth header**: confirm the app's `Authorization: Bearer`
  format is actually what Shade's API key expects on the first real run.
- **Shade Source Link**: not auto-populated yet in Notion — would need a
  known mapping from workspace/project slug to build the
  `app.shade.inc/buoyant/a/{project}/{asset_id}` URL. Can be added once
  that mapping is confirmed, or filled in by hand for now.
- **Folder paths** live in the UI, not hardcoded — if Shade's folder
  structure changes, just paste the new path next time.
