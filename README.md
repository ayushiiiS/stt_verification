# Call Transcript Review Tool

A local web app for reviewing and correcting transcripts for the **first 1,000** Muthoot call recordings. Compare **Sarvam STT** output with an editable **final** transcript.

## Quick start

```bash
cd golden_set
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5050**

## Sarvam STT generation

Set your API key in `.env`, then generate Sarvam transcripts:

```bash
cp .env.example .env
# edit .env and set SARVAM_API_KEY
python generate_sarvam_transcripts.py --resume --workers 5
```

Options:

```bash
python generate_sarvam_transcripts.py --limit 1000 --resume
python generate_sarvam_transcripts.py --call-id <call_id>
```

Generated transcripts are saved to `sarvam_transcripts.json`. The UI reads this file on startup.

Optional overrides:

```bash
export SARVAM_MODEL="saaras:v3"
export SARVAM_STT_MODE="codemix"   # transcribe | translate | verbatim | translit | codemix
export SARVAM_REQUEST_INTERVAL_SEC=1   # min seconds between Sarvam STT job starts (shared across workers)
export SARVAM_PARALLEL_WORKERS=5       # concurrent transcription jobs
```

## What it does

- Loads only the **first 1,000 calls** (sorted by call ID)
- Play call audio from signed GCS URLs
- **Sarvam STT** — speech-to-text from Sarvam Saaras v3 (batch API with diarization)
- **Final** — editable transcript, defaults to Sarvam (falls back to original structure if STT not generated)
- Save final transcript to `corrected_transcripts.json`
- Reset final back to Sarvam output

Keyboard shortcut: **Cmd/Ctrl + S** to save.

## Data sources

| File | Purpose |
|------|---------|
| `muthoot_with_public_urls .csv` | Call IDs and playable audio URLs |
| `ai-agents-production.transcripts.json` | Original transcripts (structure reference) |
| `sarvam_transcripts.json` | Sarvam STT output |
| `corrected_transcripts.json` | Saved final transcripts |

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         Browser UI                           │
│        templates/index.html · static/app.js · style.css      │
└────────────────────────────┬─────────────────────────────────┘
                             │ REST API
┌────────────────────────────▼─────────────────────────────────┐
│                        Flask (app.py)                        │
│  Loads first 1,000 calls only                                │
│  transcript_utils.py — filter, align, defaults               │
└──────┬──────────────────────────────┬────────────────────────┘
       │                              │
       ▼                              ▼
 Source CSV + JSON          sarvam_transcripts.json
                                      ▲
                                      │
                     generate_sarvam_transcripts.py
                            sarvam_stt.py → Sarvam API
```

### Modules

| Module | Role |
|--------|------|
| `app.py` | Flask server and REST API (1,000 calls) |
| `transcript_utils.py` | Message filtering and STT alignment |
| `sarvam_stt.py` | Download audio + Sarvam batch STT with diarization |
| `generate_sarvam_transcripts.py` | Batch-generate Sarvam transcripts |

### API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main UI |
| `/api/stats` | GET | Totals and Sarvam STT progress |
| `/api/calls` | GET | Paginated call list |
| `/api/calls/<id>` | GET | Sarvam STT, final, and message structure |
| `/api/calls/<id>/correct` | POST | Save final transcript |
| `/api/calls/<id>/correct` | DELETE | Reset final to Sarvam/default |

### Persistence

- Sarvam transcripts are generated offline and never modified by the UI.
- Only the **final** transcript is saved to `corrected_transcripts.json`.
- If no final save exists, final defaults to Sarvam (when available).

## Project layout

```
golden_set/
├── app.py
├── transcript_utils.py
├── sarvam_stt.py
├── generate_sarvam_transcripts.py
├── requirements.txt
├── templates/index.html
├── static/
│   ├── app.js
│   └── style.css
├── ai-agents-production.transcripts.json
├── muthoot_with_public_urls .csv
├── sarvam_transcripts.json          # generated
└── corrected_transcripts.json       # generated on save
```
# stt_verification
