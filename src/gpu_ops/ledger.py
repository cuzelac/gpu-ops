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
        """The latest event per run. Streamed: ledger.jsonl is append-only and never truncated."""
        state: dict[str, dict] = {}
        if not self.path.exists():
            return state
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    state.setdefault(rec["run_id"], {}).update(rec)
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue  # a torn last line, or an event from a version we do not know
        return state

    def run_dir(self, run_id: str) -> Path:
        """Where a run's files live. Validates the id; creates nothing.

        `gpu-ops logs` reaches this with a caller-supplied id, and a free, read-only
        command must not leave an empty directory behind for an id that never ran.
        """
        if not is_run_id(run_id):
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"{run_id!r} is not a run id")
        return self.root / "runs" / run_id

    def ensure_run_dir(self, run_id: str) -> Path:
        d = self.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def records_with_paths(self) -> list[tuple[Path, dict]]:
        """Every readable record, with the file it came from.

        A writer needs the path, not the record's `run_id` field: the two can disagree, and
        resolving the path from the field would write a second file and leave the first one
        unpatched forever.
        """
        out = []
        for p in sorted((self.root / "runs").glob("*/record.json")):
            try:
                out.append((p, json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def records(self) -> list[dict]:
        return [rec for _, rec in self.records_with_paths()]

    def write_record(self, record: "RunRecord") -> "RunRecord":
        """Persist a record, redacted per field, and return exactly what was written.

        The one place `scrub_record` is applied. Callers use the returned record rather than
        redacting their own copy, so there is a single applier as well as a single policy.
        """
        if self.redactor is not None:
            record = self.redactor.scrub_record(record)
        self._write(record.run_id, record.to_dict())
        return record

    def patch_record(self, path: Path, **fields: Any) -> Path:
        """Update these fields of the record at `path`, leaving every other key untouched.

        Takes the file, not a run id: `records_with_paths` found it by globbing, and a
        record whose `run_id` field disagrees with its directory would otherwise be copied
        to a second file and never patched.

        Reads and writes the raw JSON rather than rebuilding a RunRecord: a key this
        version does not know — one a newer gpu-ops wrote, or one since removed from the
        dataclass — must survive a rewrite instead of being silently dropped.
        """
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        raw.update(fields if self.redactor is None else self.redactor.scrub(fields))
        return self._write_json(path, raw)

    def _write(self, run_id: str, data: dict) -> Path:
        return self._write_json(self.ensure_run_dir(run_id) / "record.json", data)

    @staticmethod
    def _write_json(path: Path, data: dict) -> Path:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return path


@dataclass
class RunRecord:
    run_id: str
    job: str
    project: str
    kind: str
    spec_path: str
    status: str = "started"  # started | ok | failed
    exit_code: int | None = None
    error: str | None = field(default=None, metadata={"redact": "value"})
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
    # keys are paths the job chose when it wrote the files, so they are data, like report's
    outputs_manifest: dict[str, str] = field(default_factory=dict,
                                             metadata={"redact": "keys+values"})
    outputs_verified: bool | None = None
    teardown_confirmed: bool | None = None
    notes: list[str] = field(default_factory=list, metadata={"redact": "value"})
    # the job chose these keys, so they are data and are redacted like any value
    report: dict | None = field(default=None, metadata={"redact": "keys+values"})

    def to_dict(self) -> dict:
        return asdict(self)
