"""What a run tells the caller: the compact result, the job log, and `--get` field selection.

Agents pay for every byte they read, so `run` prints a short summary by default and
`--full` prints the whole record. Two rules live here, in one place each, because every
site that re-derived them got one of them wrong:

- **The result is bounded.** Its size must not grow with the job's output: a directory
  output collapses to its total bytes and file count, however many files it holds.
- **Read only what you need.** Nothing here materializes a whole file to return a slice
  of it; a job.log is a stream, and a report file is read up to its cap.

Error policy for the readers here: an unreadable file is a `GpuOpsError` (exit 2), not a
bare OSError, so a caller never sees "internal error" for a missing file. `tail_lines` is
the one exception and returns `[]`, because a failed run that never wrote a job.log is
ordinary and its tail is decoration, not the answer.

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


def result(code: "int | ExitCode", **fields: Any) -> dict:
    """Every command's result object.

    `ok` and `exit_code` are supplied here rather than typed into ten literal dicts, so a
    command cannot forget them and AGENTS.md's "always has ok and exit_code" stays true by
    construction.
    """
    return {"ok": int(code) == int(ExitCode.OK), "exit_code": int(code), **fields}


def compact(rec: dict, run_dir: Path, declared_outputs: list[str]) -> dict:
    """The default `run` result: what an agent needs to decide what to do next.

    `rec` is a run record as a dict (`RunRecord.to_dict()`, or a parsed record.json).
    No per-file hashes, no inputs manifest, no phase timings — those are in `--full`.

    `declared_outputs` is the spec's `[[outputs]]` paths. It is required, not optional:
    sizing per declared output is the only thing that keeps `outputs` from growing with the
    number of files the job wrote, and an optional bound is one a caller can silently skip.
    """
    out_dir = run_dir / "out"
    gpu = rec.get("gpu_name") or "?"
    if (rec.get("gpu_count") or 1) > 1:
        gpu = f"{gpu} x{rec['gpu_count']}"
    secs = rec.get("billed_seconds")
    ok = rec.get("exit_code") == 0
    # every key is always present: a caller that must guard each lookup is a caller that breaks.
    # `ok` and `exit_code` are not here — `result()` supplies those for every command alike.
    return {
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


def output_sizes(out_dir: Path, manifest: dict[str, str], declared: list[str]) -> dict[str, Any]:
    """Each declared output and its size in bytes. Sizes, not hashes: hashes are in `--full`.

    A directory collapses to `{"bytes": n, "files": m}`: one entry per file would make the
    result grow with the job's output, which is what the compact result exists to avoid.
    Outputs cannot overlap (the spec forbids it), so no file is counted twice.
    """
    sizes: dict[str, Any] = {}
    for path in declared:
        members = [k for k in manifest if k == path or k.startswith(path + "/")]
        if not members:
            continue  # an optional output the job did not write
        if members == [path]:
            sizes[path] = _size(out_dir / path)
            continue
        found = [_size(out_dir / m) for m in members]
        # bytes is null when any member could not be read, so 0 means "all empty" and never
        # "some were unreadable" — the same meaning None has for a single file, and one fixed
        # key set either way
        sizes[path] = {"bytes": None if None in found else sum(found), "files": len(members)}
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
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            return f.read(limit), size
    except OSError as e:
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"cannot read {path}: {e}")


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


def _depth(at: str) -> int:
    """How far into the result a path fragment reached. "" is the top level."""
    return 0 if not at else at.count(".") + 1


def _dig(obj: Any, path: str) -> Any:
    """Resolve one dotted path, or raise exit 2 — never a bare exception."""
    value, at, options = _resolve(obj, path, "")
    if value is _MISSING:
        where = f" at {at!r}" if at else ""
        raise GpuOpsError(ExitCode.SPEC_INVALID,
                          f"--get {path!r}: no such field{where} "
                          f"(available: {', '.join(options) or 'nothing to index'})")
    return value


def _resolve(cur: Any, rest: str, at: str) -> tuple[Any, str, list[str]]:
    """Returns (value, where-it-failed, what-was-available-there).

    The failure context is returned rather than accumulated in a shared holder: the search
    backtracks, so a holder would keep the branch that was abandoned and the error would
    name a subtree the caller never asked about.
    """
    if not rest:
        return cur, at, []
    if isinstance(cur, dict):
        parts = rest.split(".")
        best_at, best_options = at, sorted(map(str, cur))
        for i in range(len(parts), 0, -1):  # longest prefix first: names contain dots
            head, tail = ".".join(parts[:i]), ".".join(parts[i:])
            if head not in cur:
                continue
            got, deeper_at, deeper = _resolve(cur[head], tail, f"{at}.{head}" if at else head)
            if got is not _MISSING:
                return got, "", []  # a longer prefix that dead-ends falls back to a shorter one
            # report the deepest point any branch reached, not the last one tried
            if deeper and _depth(deeper_at) > _depth(best_at):
                best_at, best_options = deeper_at, deeper
        return _MISSING, best_at, best_options
    if isinstance(cur, list):
        head, _, tail = rest.partition(".")
        options = [f"indexes 0..{len(cur) - 1}"] if cur else ["an empty list"]
        if not _INDEX.fullmatch(head) or not -len(cur) <= int(head) < len(cur):
            return _MISSING, at, options
        return _resolve(cur[int(head)], tail, f"{at}.{head}" if at else head)
    return _MISSING, at, [f"a {type(cur).__name__}, which has no fields"]


def dumps(obj: Any) -> str:
    """The one way gpu-ops serializes a result."""
    return json.dumps(obj, indent=2, default=str)
