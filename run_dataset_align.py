#!/usr/bin/env python3
"""Run MMS_FA forced alignment for a dataset (per-speaker tracks).

Wraps handoff/align_service/batch_align_spinny.py using the dataset's calls.json
and writes aligned_timings.json into uploads/<dataset>/.

For Karan Spinny (separate human/agent tracks):
  .venv/bin/python3 run_dataset_align.py karan-spinny

Requires align_service deps:
  cd handoff/align_service && python3 -m venv .venv && pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HANDOFF_DIR = BASE_DIR / "handoff"
ALIGN_DIR = HANDOFF_DIR / "align_service"
UPLOADS_DIR = BASE_DIR / "uploads"

DATASET_CALLS = {
    "karan-spinny": ("data/spinny-karan.json", "all_data/spinny_aligned_timings.json"),
    "indiamart": ("data/indiamart.json", "all_data/indiamart_aligned_timings.json"),
    "muthoot": ("data/muthoot.json", "all_data/muthoot_aligned_timings.json"),
}


def resolve_input(dataset: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    calls = UPLOADS_DIR / dataset / "calls.json"
    if calls.exists():
        return calls
    rel, _ = DATASET_CALLS.get(dataset, (f"data/{dataset}.json", ""))
    candidate = BASE_DIR / rel
    if candidate.exists():
        return candidate
    handoff_calls = HANDOFF_DIR / "all_data" / Path(rel).name
    if handoff_calls.exists():
        return handoff_calls
    raise SystemExit(f"No calls JSON found for dataset {dataset!r}")


def resolve_output(dataset: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return UPLOADS_DIR / dataset / "aligned_timings.json"


def align_python() -> Path:
    venv_py = ALIGN_DIR / ".venv" / "bin" / "python3"
    if venv_py.exists():
        return venv_py
    return Path(sys.executable)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Dataset id (e.g. karan-spinny)")
    parser.add_argument("--input", help="Override calls JSON path")
    parser.add_argument("--out", help="Override aligned_timings.json output path")
    parser.add_argument("--pad-s", type=float, default=0.15)
    parser.add_argument("--workers", type=int, default=1, help="Shard count for parallel runs")
    parser.add_argument("--shard", type=int, default=0, help="This worker's shard index (0-based)")
    parser.add_argument("extra", nargs="*", help="Extra args passed to batch_align_spinny.py")
    args = parser.parse_args()

    input_path = resolve_input(args.dataset, args.input)
    out_path = resolve_output(args.dataset, args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    script = ALIGN_DIR / "batch_align_spinny.py"
    if not script.exists():
        raise SystemExit(f"Missing {script}")

    cmd = [
        str(align_python()),
        str(script),
        "--input",
        str(input_path),
        "--out",
        str(out_path),
        "--pad-s",
        str(args.pad_s),
    ]
    if args.workers > 1:
        cmd.extend(["--num-shards", str(args.workers), "--shard-index", str(args.shard)])
    cmd.extend(args.extra)

    print("Running:", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ALIGN_DIR, check=True)

    # Mirror into handoff bootstrap path when using default output layout.
    bootstrap_rel = DATASET_CALLS.get(args.dataset, ("", ""))[1]
    if bootstrap_rel and out_path == UPLOADS_DIR / args.dataset / "aligned_timings.json":
        bootstrap = HANDOFF_DIR / bootstrap_rel
        bootstrap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, bootstrap)
        print(f"Copied bootstrap -> {bootstrap}", flush=True)

    try:
        import load_env  # noqa: F401
        import gcs_storage

        gcs_storage.push_dataset_file(UPLOADS_DIR, args.dataset, "aligned_timings.json")
        print("Pushed aligned_timings.json to GCS", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"GCS push skipped: {exc}", flush=True)


if __name__ == "__main__":
    main()
