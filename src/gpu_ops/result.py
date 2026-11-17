"""What a run tells the caller: the compact result, the job log, and `--get` field selection.

Agents pay for every byte they read, so `run` prints a short summary by default and
`--full` prints the whole record. Two rules live here, in one place each, because every
site that re-derived them got one of them wrong:

- **The result is bounded.** Its size must not grow with the job's output: a directory
  output collapses to its total bytes and file count, however many files it holds.
- **Read only what you need.** Nothing here materializes a whole file to return a slice
  of it; a job.log is a stream, and a report file is read up to its cap.

The compact shape is part of the CLI contract (AGENTS.md): add fields, never rename them,
and keep the key set the same on every run so a caller can index it without guarding.
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

from .errors import ExitCode, GpuOpsError

LOG_TAIL_LINES = 20  # lines of job.log included in a failed run's result
DEFAULT_LOG_TAIL = 200  # lines `gpu-ops logs` returns when no --tail is given
PHASE_MARKER = "[gpu-ops] phase: "  # written into job.log so `logs --phase` can split it
_INDEX = re.compile(r"-?\d+")


def compact(rec: dict, run_dir: Path, declared_outputs: list[str] | None = None) -> dict:
    """The default `run` result: what an agent needs to decide what to do next.

    `rec` is a run record as a dict (`RunRecord.to_dict()`, or a parsed record.json).
    No per-file hashes, no inputs manifest, no phase timings — those are in `--full`.

    `declared_outputs` is the spec's `[[outputs]]` paths, which is what keeps `outputs`
    bounded; without them (a record read back without its spec) each verified file is
    listed instead.
    """
    out_dir = run_dir / "out"
    gpu = rec.get("gpu_name") or "?"
    if (rec.get("gpu_count") or 1) > 1:
        gpu = f"{gpu} x{rec['gpu_count']}"
    secs = rec.get("billed_seconds")
    ok = rec.get("exit_code") == 0
    # every key is always present: a caller that must guard each lookup is a caller that breaks
    return {
        "ok": ok,
        "exit_code": rec.get("exit_code"),
        "run_id": rec.get("run_id"),
        "status": rec.get("status"),
        "error": rec.get("error"),
        "gpu": gpu,
        "tier": rec.get("tier"),
        "price_per_hr": rec.get("price_per_hr"),
        "minutes": round(secs / 60, 2) if secs is not None else None,
        "cost_estimate_usd": rec.get("cost_estimate_usd"),
        "teardown_confirmed": rec.get("teardown_confirmed"),
        "outputs_verified": rec.get("outputs_verified"),
        "outputs_dir": str(out_dir),
        "outputs": output_sizes(out_dir, rec.get("outputs_manifest") or {}, declared_outputs),
        "deadman": rec.get("deadman"),
        "notes": rec.get("notes") or [],
        "report": rec.get("report"),  # null when the spec asked for none
        "log_tail": None if ok else tail_lines(run_dir / "job.log", LOG_TAIL_LINES),
    }


def output_sizes(out_dir: Path, manifest: dict[str, str],
                 declared: list[str] | None = None) -> dict[str, Any]:
    """Each output and its size in bytes. Sizes, not hashes: hashes are in `--full`.

    A declared output that is a directory collapses to `{"bytes": n, "files": n}` — listing
    one entry per file would make the result grow with the job's output, which is the whole
    thing the compact result exists to avoid.
    """
    if not declared:
        return {rel: _size(out_dir / rel) for rel in sorted(manifest)}
    sizes: dict[str, Any] = {}
    for path in declared:
        members = [k for k in manifest if k == path or k.startswith(path + "/")]
        if not members:
            continue  # an optional output the job did not write
        if members == [path]:
            sizes[path] = _size(out_dir / path)
        else:
            total = sum(s for s in (_size(out_dir / m) for m in members) if s is not None)
            sizes[path] = {"bytes": total, "files": len(members)}
    return sizes


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def tail_lines(path: Path, n: int) -> list[str]:
    """Last n lines of a log file, read with a bounded buffer. Missing file -> [].

    A QLoRA run's job.log is pip output plus thousands of progress lines; reading it whole
    to return 20 lines is exactly the waste this module exists to remove.
    """
    if n <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=n)]
    except OSError:
        return []


def read_head_bytes(path: Path, limit: int) -> tuple[bytes, int]:
    """The first `limit` bytes, and the file's real size (so the caller can say "truncated")."""
    size = path.stat().st_size
    with path.open("rb") as f:
        return f.read(limit), size


def read_log(path: Path, *, tail: int = DEFAULT_LOG_TAIL, grep: str | None = None,
             phase: str | None = None) -> tuple[list[str], int, int]:
    """Filter a job.log. Returns (lines, lines_in_file, lines_matched).

    `phase` keeps only the lines between that phase's marker and the next one.
    `tail` keeps the last n matched lines; 0 means all of them. Streamed, holding at most
    `tail` lines in memory.
    """
    if tail < 0:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"--tail must be 0 (all lines) or more, not {tail}")
    rx = None
    if grep:
        try:
            rx = re.compile(grep)
        except re.error as e:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"--grep is not a valid regex: {e}")
    kept: deque[str] | list[str] = deque(maxlen=tail) if tail > 0 else []
    in_file = matched = 0
    keeping = phase is None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                in_file += 1
                line = raw.rstrip("\n")
                if phase and line.startswith(PHASE_MARKER):
                    keeping = line[len(PHASE_MARKER):].strip() == phase
                    continue
                if not keeping or (rx is not None and not rx.search(line)):
                    continue
                matched += 1
                kept.append(line)
    except OSError as e:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"cannot read {path}: {e}")
    return list(kept), in_file, matched


def select(result: dict, paths: str) -> dict:
    """`--get a.b,c.0.d` -> {"a.b": …, "c.0.d": …}. A path that does not exist is exit 2.

    A dotted path is matched key by key, except that a key containing dots (an output or
    report file name) matches as a whole: `report.lora/train-record.json` works, and so
    does drilling further into it.
    """
    out: dict[str, Any] = {}
    for raw in paths.split(","):
        p = raw.strip()
        if p:
            out[p] = _dig(result, p)
    if not out:
        raise GpuOpsError(ExitCode.SPEC_INVALID, "--get needs at least one field path")
    return out


_MISSING = object()


def _dig(obj: Any, path: str) -> Any:
    """Resolve one dotted path, or raise exit 2 — never a bare exception."""
    deepest = {"at": "", "keys": sorted(map(str, obj)) if isinstance(obj, dict) else []}
    value = _resolve(obj, path, deepest)
    if value is _MISSING:
        where = f" at {deepest['at']!r}" if deepest["at"] else ""
        available = ", ".join(deepest["keys"]) or "nothing to index"
        raise GpuOpsError(ExitCode.SPEC_INVALID,
                          f"--get {path!r}: no such field{where} (available: {available})")
    return value


def _resolve(cur: Any, rest: str, deepest: dict) -> Any:
    if not rest:
        return cur
    if isinstance(cur, dict):
        parts = rest.split(".")
        for i in range(len(parts), 0, -1):  # longest prefix first: a file name may contain dots
            head = ".".join(parts[:i])
            if head not in cur:
                continue
            tail = ".".join(parts[i:])
            if tail and isinstance(cur[head], dict):
                deepest.update(at=head, keys=sorted(map(str, cur[head])))
            got = _resolve(cur[head], tail, deepest)
            if got is not _MISSING:
                return got  # a longer prefix that dead-ends falls back to a shorter one
        return _MISSING
    if isinstance(cur, list):
        head, _, tail = rest.partition(".")
        if not _INDEX.fullmatch(head) or not -len(cur) <= int(head) < len(cur):
            return _MISSING
        return _resolve(cur[int(head)], tail, deepest)
    return _MISSING


def dumps(obj: Any) -> str:
    """The one way gpu-ops serializes a result."""
    return json.dumps(obj, indent=2, default=str)
