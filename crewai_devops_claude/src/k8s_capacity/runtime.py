"""Run workspace shared by tools, guardrails and the report assembler.

A run directory (env CAPACITY_RUN_DIR) holds:
  run_config.json        RunConfig (written by main.py before kickoff)
  snapshot.json          collected or copied snapshot (single source of truth for all numbers)
  results/<tool>.json    full output of every tool call (the LLM only sees a compact view)
  report.md / report.json final artifacts
Large JSON never travels through the LLM: tools read the snapshot from disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .engine.snapshot import Context, load_snapshot
from .models import RunConfig

_CTX_CACHE: dict[str, tuple[float, Context]] = {}


def run_dir() -> Path:
    d = os.environ.get("CAPACITY_RUN_DIR")
    if not d:
        raise RuntimeError("CAPACITY_RUN_DIR is not set (start the crew via k8s_capacity.main)")
    return Path(d)


def init_run(cfg: RunConfig, base: str | Path = "runs") -> Path:
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = Path(base) / f"{stamp}-{cfg.mode}-{cfg.cluster_name}"
    (d / "results").mkdir(parents=True, exist_ok=True)
    (d / "run_config.json").write_text(cfg.model_dump_json(indent=2))
    os.environ["CAPACITY_RUN_DIR"] = str(d.resolve())
    if cfg.snapshot_file:
        shutil.copyfile(cfg.snapshot_file, d / "snapshot.json")
    return d


def config() -> RunConfig:
    return RunConfig.model_validate_json((run_dir() / "run_config.json").read_text())


def snapshot_path() -> Path:
    return run_dir() / "snapshot.json"


def context() -> Context:
    p = snapshot_path()
    if not p.exists():
        raise FileNotFoundError("snapshot.json missing: run collect_cluster_snapshot first")
    mtime = p.stat().st_mtime
    key = str(p)
    if key not in _CTX_CACHE or _CTX_CACHE[key][0] != mtime:
        _CTX_CACHE[key] = (mtime, load_snapshot(p, scoring=config().scoring))
    return _CTX_CACHE[key][1]


def save_result(name: str, data: dict) -> str:
    path = run_dir() / "results" / f"{name}.json"
    path.write_text(json.dumps(data, indent=1, default=str))
    return str(path.relative_to(run_dir()))


def load_result(name: str) -> dict | None:
    path = run_dir() / "results" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def snapshot_digest() -> str:
    return hashlib.sha256(snapshot_path().read_bytes()).hexdigest()[:12]
