# Call Transcript Review Tool

A local Flask web app for reviewing call transcripts across multiple clients. The UI lets reviewers upload transcript JSON files, listen to audio with a waveform, edit the final transcript, convert Latin Hindi to Devanagari, use repeated phrase suggestions, and require verification by a second user.

## Persistence (Google Cloud Storage)

Uploaded calls, saved/verified finals, Sarvam STT output, progress, and users are synced to GCS so data survives deploys.

Default bucket: **`gotldenset`**

Layout:

```
gs://gotldenset/
  users.json
  indiamart/
    calls.json
    corrected_transcripts.json
    sarvam_transcripts.json
    stt_progress.json
  spinny/
    ...
  amc/
  abhfl/
  amber/
```

Each client tab maps to a folder with the same name.

Env:

```bash
GCS_BUCKET=gotldenset
GCS_LOCATION=asia-south1
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
# Or for Vercel / serverless:
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

On startup the app pulls these objects into local `uploads/`. Every save/upload/STT/user write also pushes back to the matching GCS object. If the bucket does not exist and credentials allow it, the app creates `gotldenset`.

Check status at `/api/storage` (while logged in).

## Authentication

Open `/login` to **Log in** or **Sign up**.

Seeded team accounts still work by default (password = username unless overridden):
`ayushi`, `kriti`, `akash`, `yash`.

New accounts created via Sign up are stored in `uploads/users.json`.

Save and verify use the logged-in username. A final saved by one user must be verified by a different logged-in user.

Optional env:

```bash
FLASK_SECRET_KEY=change-me-to-a-long-random-string
AUTH_PASSWORDS=ayushi:secret,kriti:secret,akash:secret,yash:secret
```

## Quick Start

```bash
cd golden_set
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run The UI

```bash
source .venv/bin/activate
python3 app.py
```

Then open **http://127.0.0.1:5050**

Optional port override:

```bash
PORT=5050 python3 app.py
```

## Main Features

- Five client tabs: **IndiaMART**, **Spinny**, **AMC**, **ABHFL**, and **Amber**.
- JSON upload for each client tab. The uploaded file should include transcripts and playable audio URLs.
- Waveform audio player using `wavesurfer.js`, with playback speeds from `0.75x` to `2x`.
- Transcript and audio sync: active transcript turns highlight during playback when timing data is available.
- Click a transcript turn timestamp to seek the audio to that turn.
- Final transcript editor with Latin Hindi to Devanagari conversion: type a word like `namaste`, then press `Tab`.
- Phrase recommender built from repeated saved final transcript words and phrases.
- Second-user verification flow: one user saves, a different user must verify.
- **Start Sarvam STT** from the UI for any client tab after upload.
- **Export verified** downloads numbered JSON with `callLogId` for all verified finals.
- Keyboard shortcut: **Cmd/Ctrl + S** saves the final transcript.
- Finals are **not saved** until you click Save. Drafts can start from Sarvam STT.

## Reviewer Workflow

1. Enter your name in the **Your name** field.
2. Select a client tab.
3. Upload a JSON file if that tab has no calls yet.
4. Select a call from the left sidebar.
5. Play audio, review the waveform, and edit the **Final** transcript.
6. Use `Tab` inside the final transcript textarea to convert the current Latin Hindi word to Devanagari.
7. Save the final transcript.
8. Ask another reviewer to enter their own name and click **Verify**.

Verification is name-based, not full authentication. The verifier name must be different from the editor name stored on the saved transcript.

## JSON Upload Format

Uploads can be either an object keyed by call ID or an array of call objects.

Object format:

```json
{
  "call_001": {
    "callLogId": "call_001",
    "public_url": "https://example.com/audio.mp3",
    "messages": [
      {
        "_id": "turn_1",
        "role": "assistant",
        "type": "message",
        "content": "namaste, kaise hain aap?"
      },
      {
        "_id": "turn_2",
        "role": "user",
        "type": "message",
        "content": "theek hun"
      }
    ]
  }
}
```

Array format:

```json
[
  {
    "callLogId": "call_001",
    "public_url": "https://example.com/audio.mp3",
    "messages": []
  }
]
```

Supported fields:

| Field | Purpose |
|------|---------|
| `callLogId`, `id`, or `_id` | Call identifier |
| `public_url`, `url`, or `recordingUrl` | Playable audio URL |
| `messages` | Transcript turns shown in Original/Final |
| `stt_messages` or `sarvam_messages` | Optional STT turns |
| `segments` or `stt_segments` | Optional STT segments |
| `timings` | Optional turn timings for audio sync |
| `raw.diarized_transcript.entries` | Optional Sarvam-style timing entries |

Timing entries should contain `start_time_seconds` and `end_time_seconds`. If timings are missing, audio still plays but transcript turns cannot auto-highlight by time.

## Existing Data

The app currently bootstraps IndiaMART from files in `all_data/` when available:

| File | Purpose |
|------|---------|
| `all_data/indiamart_63_transcripts.json` | Original IndiaMART transcripts |
| `all_data/indiamart_final63_public_urls.csv` | IndiaMART playable audio URLs |
| `all_data/indiamart_sarvam_transcripts.json` | Sarvam STT with diarized timing data |
| `all_data/indiamart_corrected_transcripts.json` | Saved final transcripts |
| `all_data/indiamart_final_with_public_urls.json` | Optional final export with public URLs |

Uploaded data for other clients is written under `uploads/<dataset>/`.

## Sarvam STT Generation

Set your API key in `.env`, then generate Sarvam transcripts:

```bash
cp .env.example .env
# edit .env and set SARVAM_API_KEY
python3 generate_indiamart_sarvam_transcripts.py --resume --workers 5
```

Older Muthoot generation scripts are still present:

```bash
python3 generate_sarvam_transcripts.py --limit 1000 --resume
python3 generate_sarvam_transcripts.py --call-id <call_id>
```

Optional overrides:

```bash
export SARVAM_MODEL="saaras:v3"
export SARVAM_STT_MODE="codemix"          # transcribe | translate | verbatim | translit | codemix
export SARVAM_REQUEST_INTERVAL_SEC=1      # min seconds between STT job starts
export SARVAM_PARALLEL_WORKERS=5          # concurrent transcription jobs
```

## Architecture

```
Browser UI
  templates/index.html
  static/app.js
  static/style.css
      |
      | REST API
      v
Flask app.py
  - Dataset tabs and uploaded JSON ingest
  - Call list, call detail, save/reset/verify APIs
  - Sarvam timing passthrough for transcript/audio sync
  - Phrase recommendation index
  - Latin Hindi transliteration proxy
      |
      v
Local JSON/CSV files + uploads/<dataset>/
```

### Modules

| Module | Role |
|--------|------|
| `app.py` | Flask server, dataset loading, upload ingest, save/reset/verify APIs |
| `transcript_utils.py` | Message filtering, STT alignment, final message cleaning |
| `json_format.py` | Pretty numbered JSON writer |
| `sarvam_stt.py` | Download audio and call Sarvam STT |
| `generate_indiamart_sarvam_transcripts.py` | Batch-generate IndiaMART Sarvam transcripts |
| `generate_sarvam_transcripts.py` | Legacy Muthoot Sarvam generation |
| `templates/index.html` | Single-page app shell |
| `static/app.js` | Browser state, upload, waveform, sync, transliteration, verification |
| `static/style.css` | UI layout and styling |

### API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main UI |
| `/api/datasets` | GET | Dataset labels and totals |
| `/api/stats?dataset=<id>` | GET | Totals, pending, awaiting verification, verified, STT progress |
| `/api/calls?dataset=<id>` | GET | Paginated call list |
| `/api/calls/<id>?dataset=<id>` | GET | Full call payload with original, STT, final, timings, and status |
| `/api/upload?dataset=<id>` | POST | Upload JSON file/body into the active dataset |
| `/api/stt/start?dataset=<id>` | POST | Start Sarvam STT for calls in the active dataset |
| `/api/stt/status?dataset=<id>` | GET | STT job status/progress |
| `/api/export/verified?dataset=<id>` | GET | Download numbered verified finals JSON |
| `/api/calls/<id>/correct?dataset=<id>` | POST | Save final transcript with `reviewer` |
| `/api/calls/<id>/correct?dataset=<id>` | DELETE | Reset final transcript |
| `/api/calls/<id>/verify?dataset=<id>` | POST | Verify final transcript with a different `verifier` |
| `/api/phrases?dataset=<id>&q=<text>` | GET | Repeated phrase suggestions |
| `/api/transliterate` | POST | Convert Latin Hindi text to Devanagari |

### Persistence

- Uploaded calls are saved to `uploads/<dataset>/calls.json`.
- Saved final transcripts are saved to `uploads/<dataset>/corrected_transcripts.json` for uploaded datasets.
- IndiaMART can also read existing files from `all_data/`.
- Saving a final transcript clears any prior verification so the final must be verified again.
- Sarvam transcripts are generated offline and are not modified by the UI.

### Network Notes

- The waveform library is loaded from `https://unpkg.com/wavesurfer.js@7/dist/wavesurfer.min.js`.
- Latin Hindi to Devanagari conversion calls Google Input Tools through `/api/transliterate`.
- Audio waveform rendering may fail if an audio URL is expired or blocked by CORS; in that case the UI shows a fallback message.

## Project Layout

```
golden_set/
├── app.py
├── transcript_utils.py
├── json_format.py
├── sarvam_stt.py
├── generate_indiamart_sarvam_transcripts.py
├── generate_sarvam_transcripts.py
├── requirements.txt
├── templates/
│   └── index.html
├── static/
│   ├── app.js
│   └── style.css
├── all_data/
│   ├── indiamart_63_transcripts.json
│   ├── indiamart_final63_public_urls.csv
│   ├── indiamart_sarvam_transcripts.json
│   └── indiamart_corrected_transcripts.json
└── uploads/
    └── <dataset>/
        ├── calls.json
        └── corrected_transcripts.json
```
