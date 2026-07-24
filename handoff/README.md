# Forced-Alignment Handoff

All files created/modified for the MMS_FA audio↔transcript timing feature.
Baseline: repo commit `c9834f9`. **No credentials are included** (keep
`service-account.json` out of any share).

## Pipeline (how timings are produced)

```
source transcript + separate human/agent audio URLs
   │
   ├─ 1. batch_align_spinny.py  → MMS_FA forced alignment, per speaker per track
   │        (forced_align.py is the model core)                → *.before_tighten.json
   │
   ├─ 2. tighten_starts.py      → VAD (Silero) speech-island detection +
   │        monotonic snapping + QA report                     → indiamart_aligned_timings.json
   │
   └─ 3. split_merged_turns.py  → split diarization-merged user boxes
            (only when audio agrees), then re-run 1+2
```
The Flask app (`app.py`) maps these timings onto transcript turns by position.

## Files

### Modified app files (vs baseline)
- `app.py` – load dataset + FA timings, map timings→turns, strip SSML tags
- `transcript_utils.py` – `clean_transcript_text()` SSML stripper
- `static/app.js`, `templates/index.html` – UI wiring
- `.env.example` – GCS / service-account env vars

### align_service/ (new — the FA service)
- `forced_align.py` – MMS_FA engine (chunked emissions, word→turn spans, optional `<star>`)
- `batch_align_spinny.py` – per-call/per-speaker batch aligner (user→human, assistant→agent)
- `batch_align.py` – variant that fetches audio via GCS service account
- `tighten_starts.py` – Silero/energy VAD tightening (`--vad silero`), monotonic island assignment, QA report
- `split_merged_turns.py` – conservative merged-turn detector/splitter
- `app.py` – FastAPI microservice for on-demand alignment
- `requirements.txt` – dependencies

### all_data/ (data)
- `indiamart_aligned_timings.json` – **live output** (Silero + merge-splits, 146 calls)
- `indiamart_aligned_timings.before_tighten.json` – raw FA windows
- `indiamart_aligned_timings.energy_backup.json` – pre-split, energy-VAD backup
- `indiamart_qa_report.json` – flagged turns needing human review
- `indiamart (3).json` – working transcript (merge-splits applied)
- `indiamart (3).presplit_backup.json` – transcript before splits
- `spinny_aligned_timings.json`, `Muthoot_aligned_timings.json`, `spinny-karan.json`

### logs/ – run logs (reference only)

## Re-run commands

```bash
cd align_service && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Forced alignment (raw windows)
python batch_align_spinny.py --input "../all_data/indiamart (3).json" \
    --out ../all_data/indiamart_aligned_timings.before_tighten.json

# 2. VAD tightening (Silero) → live timings + QA report
python tighten_starts.py --input "../all_data/indiamart (3).json" \
    --timings ../all_data/indiamart_aligned_timings.before_tighten.json \
    --out ../all_data/indiamart_aligned_timings.json \
    --report ../all_data/indiamart_qa_report.json --vad silero

# 3. (optional) detect+split merged user turns; dry-run without --apply
python split_merged_turns.py --input "../all_data/indiamart (3).json" \
    --timings ../all_data/indiamart_aligned_timings.before_tighten.json
```

## QA report flags
- `no_island` – no speech found in the turn's window; kept raw FA time (most suspect)
- `short_island` – speech shorter than the words imply (possibly partial)
- `drift_snap` – FA missed; snapped to nearest island (verify)
