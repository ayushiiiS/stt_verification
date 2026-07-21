#!/usr/bin/env python3
"""Migrate local uploads/ into the per-call GCS layout.

  gs://<bucket>/<Agent>/<call_id>/{meta,recording,human,agent,transcripts}

Usage:
  python3 migrate_uploads_to_gcs.py
  python3 migrate_uploads_to_gcs.py --no-audio
  python3 migrate_uploads_to_gcs.py --dataset indiamart
"""

from __future__ import annotations

import argparse
import load_env  # noqa: F401
import os
from pathlib import Path

import gcs_storage

BASE_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BASE_DIR / "uploads"
DATASETS = ("indiamart", "abhfl", "amber", "muthoot")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        action="append",
        dest="datasets",
        help="Limit to one or more dataset ids (default: all)",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Skip mirroring human/agent/recording audio files",
    )
    args = parser.parse_args()

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.environ[
            "GOOGLE_APPLICATION_CREDENTIALS"
        ].strip()

    datasets = tuple(args.datasets) if args.datasets else DATASETS
    print(gcs_storage.status(), flush=True)
    if not gcs_storage.is_enabled():
        raise SystemExit("GCS is not enabled — check credentials / GCS_BUCKET")

    summary = gcs_storage.migrate_local_uploads_to_gcs(
        UPLOADS_DIR,
        datasets,
        upload_audio=not args.no_audio,
    )
    print("Done:", summary, flush=True)
    if not args.no_audio:
        print(
            "Audio uploads are running in background threads; "
            "wait until process exits after they finish.",
            flush=True,
        )
        # Give queued audio uploads time to finish before interpreter teardown
        import time

        time.sleep(2)
        pool = gcs_storage._audio_pool()
        pool.shutdown(wait=True)


if __name__ == "__main__":
    main()
