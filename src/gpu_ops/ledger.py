"""Local state: the ledger of instances and the per-run records.

state_dir/
  ledger.jsonl            append-only events; the last event per run_id is its current state
  runs/<run-id>/
    record.json           the run record (written at the end, and updated by `spend --reconcile`)
    inputs-manifest.json  every uploaded input file and its sha256 (kept out of record.json: a
                          large dataset would swamp it; the record keeps the digest and the count)
    job.log               setup + command output, secrets redacted, with phase markers
    out/                  outputs brought back

The ledger is a convenience for reports. Safety never depends on it: `ps`, `kill`
and `reap` ask the provider directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ExitCode, GpuOpsError
from .names import is_run_id
from .paths import state_dir


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Ledger:
    def __init__(self, root: Path | None = None, redactor: "Any | None" = None):
        # every record written goes through this redactor, so no caller has to remember to
        # redact its own field; the Runner wires its own in (it is the only writer with secrets)
        self.redactor = redactor
        self.root = Path(root) if root else state_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self.path = self.root / "ledger.jsonl"

    def event(self, run_id: str, status: str, **fields: Any) -> None:
        rec = {"ts": utcnow().isoformat(), "run_id": run_id, "status": status, **fields}
        line = json.dumps(rec, sort_keys=True) + "\n"
        # one write() per line + fsync: a crash leaves at most a torn last line, which current() skips
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def current(self) -> dict[str, dict]:
        state: dict[str, dict] = {}
        if not self.path.exists():
            return state
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            state.setdefault(rec["run_id"], {}).update(rec)
        return state

    def run_dir(self, run_id: str) -> Path:
        if not is_run_id(run_id):
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"{run_id!r} is not a run id")
        d = self.root / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def records(self) -> list[dict]:
        out = []
        for p in sorted((self.root / "runs").glob("*/record.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def write_record(self, record: "RunRecord") -> Path:
        return self._write(record.run_id, record.to_dict())

    def patch_record(self, run_id: str, **fields: Any) -> Path:
        """Update these fields of a record on disk, leaving every other key untouched.

        Reads and writes the raw JSON rather than rebuilding a RunRecord: a key this
        version does not know — one a newer gpu-ops wrote, or one since removed from the
        dataclass — must survive a rewrite instead of being silently dropped.
        """
        p = self.run_dir(run_id) / "record.json"
        raw = {}
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
        raw.update(fields)
        return self._write(run_id, raw)

    def _write(self, run_id: str, data: dict) -> Path:
        if self.redactor is not None:
            data = self.redactor.scrub(data)
        p = self.run_dir(run_id) / "record.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
        return p


@dataclass
class RunRecord:
    run_id: str
    job: str
    project: str
    kind: str
    spec_path: str
    status: str = "started"  # started | ok | failed
    exit_code: int | None = None
    error: str | None = None
    provider: str | None = None
    instance_id: str | None = None
    instance_name: str | None = None
    gpu_id: str | None = None
    gpu_name: str | None = None
    gpu_count: int | None = None
    vram_gb: int | None = None
    tier: str | None = None
    data_center: str | None = None
    image: str | None = None
    price_per_hr: float | None = None
    started_at: str | None = None  # when create() was called
    ended_at: str | None = None  # when teardown was confirmed (or gave up)
    billed_seconds: float | None = None
    cost_estimate_usd: float | None = None
    cost_billed_usd: float | None = None  # filled by `spend --reconcile`
    worst_case_usd: float | None = None
    deadline: str | None = None
    deadman: str | None = None  # armed | unarmed | n/a
    phases: dict[str, float] = field(default_factory=dict)  # seconds per phase
    inputs_hash: str | None = None  # one digest over the whole inputs manifest
    inputs_file_count: int | None = None
    outputs_manifest: dict[str, str] = field(default_factory=dict)
    outputs_verified: bool | None = None
    teardown_confirmed: bool | None = None
    notes: list[str] = field(default_factory=list)
    report: dict | None = None  # [report] files from the spec; None when the spec asked for none

    def to_dict(self) -> dict:
        return asdict(self)
