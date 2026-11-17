"""What a run tells the caller: the compact result, the job log, and `--get` field selection.

Agents pay for every byte they read, so `run` prints a short summary by default and
`--full` prints the whole record. The compact shape is part of the CLI contract
(AGENTS.md): add fields, never rename them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ExitCode, GpuOpsError

LOG_TAIL_LINES = 20  # lines of job.log included in a failed run's result
DEFAULT_LOG_TAIL = 200  # lines `gpu-ops logs` returns when no --tail is given
PHASE_MARKER = "[gpu-ops] phase: "  # written into job.log so `logs --phase` can split it


def compact(rec: dict, run_dir: Path) -> dict:
    """The default `run` result: what an agent needs to decide what to do next.

    `rec` is a run record as a dict (`RunRecord.to_dict()`, or a parsed record.json).
    No per-file hashes, no inputs manifest, no phase timings — those are in `--full`.
    """
    out_dir = run_dir / "out"
    gpu = rec.get("gpu_name") or "?"
    if (rec.get("gpu_count") or 1) > 1:
        gpu = f"{gpu} x{rec['gpu_count']}"
    secs = rec.get("billed_seconds")
    ok = rec.get("exit_code") == 0
    res: dict[str, Any] = {
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
        "outputs": output_sizes(out_dir, rec.get("outputs_manifest") or {}),
        "deadman": rec.get("deadman"),
        "notes": rec.get("notes") or [],
    }
    if rec.get("report") is not None:  # only when the spec has a [report] table
        res["report"] = rec["report"]
    if not ok:
        res["log_tail"] = tail_lines(run_dir / "job.log", LOG_TAIL_LINES)
    return res


def output_sizes(out_dir: Path, manifest: dict[str, str]) -> dict[str, int | None]:
    """Each verified output and its size in bytes. Sizes, not hashes: hashes are in `--full`."""
    sizes: dict[str, int | None] = {}
    for rel in sorted(manifest):
        try:
            sizes[rel] = (out_dir / rel).stat().st_size
        except OSError:
            sizes[rel] = None
    return sizes


def tail_lines(path: Path, n: int) -> list[str]:
    """Last n lines of a log file (already redacted when written). Missing file -> []."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:] if n > 0 else lines


def read_log(path: Path, *, tail: int = DEFAULT_LOG_TAIL, grep: str | None = None,
             phase: str | None = None) -> tuple[list[str], int, int]:
    """Filter a job.log. Returns (lines, lines_in_file, lines_matched).

    `phase` keeps only the lines between that phase's marker and the next one.
    `tail` keeps the last n matched lines; 0 means all of them.
    """
    try:
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"cannot read {path}: {e}")
    lines = all_lines
    if phase:
        lines, keeping = [], False
        for line in all_lines:
            if line.startswith(PHASE_MARKER):
                keeping = line[len(PHASE_MARKER):].strip() == phase
                continue
            if keeping:
                lines.append(line)
    if grep:
        try:
            rx = re.compile(grep)
        except re.error as e:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"--grep is not a valid regex: {e}")
        lines = [x for x in lines if rx.search(x)]
    matched = len(lines)
    if tail > 0:
        lines = lines[-tail:]
    return lines, len(all_lines), matched


def select(result: dict, paths: str) -> dict:
    """`--get a.b,c.0.d` -> {"a.b": …, "c.0.d": …}. A path that does not exist is exit 2.

    A dotted path is matched key by key, except that a key containing dots (an output
    or report file name) matches as a whole: `report.lora/train-record.json` works.
    """
    out: dict[str, Any] = {}
    for raw in paths.split(","):
        p = raw.strip()
        if p:
            out[p] = _dig(result, p)
    if not out:
        raise GpuOpsError(ExitCode.SPEC_INVALID, "--get needs at least one field path")
    return out


def _dig(obj: Any, path: str) -> Any:
    cur, rest = obj, path
    while True:
        if isinstance(cur, dict):
            parts = rest.split(".")
            for i in range(len(parts), 0, -1):  # longest prefix first: an output name may contain dots
                head = ".".join(parts[:i])
                if head in cur:
                    cur, rest = cur[head], ".".join(parts[i:])
                    break
            else:
                raise GpuOpsError(ExitCode.SPEC_INVALID,
                                  f"--get {path!r}: no field {parts[0]!r} here "
                                  f"(available: {', '.join(sorted(map(str, cur))) or 'none'})")
        elif isinstance(cur, list):
            head, _, rest = rest.partition(".")
            if not (head.lstrip("-").isdigit() and -len(cur) <= int(head) < len(cur)):
                raise GpuOpsError(ExitCode.SPEC_INVALID, f"--get {path!r}: {head!r} is not an index into a list "
                                                         f"of {len(cur)}")
            cur = cur[int(head)]
        else:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"--get {path!r}: {rest!r} cannot be read from a "
                                                     f"{type(cur).__name__}")
        if not rest:
            return cur


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)
