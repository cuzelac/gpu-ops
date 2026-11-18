"""The job that lives on the instance, and the outputs it leaves behind.

A detached run is collected by a *later process*, long after the one that launched it has
gone. That process has no job spec: the spec lives in the calling project, which may have
moved, changed or been deleted in the meantime. So everything collection needs is a type
that can be written down at launch and read back later — `CollectPlan` — and the only way
to get one from disk is `CollectPlan.load`. A caller cannot reach for the spec instead,
because by then there is nothing to reach for.

The same functions serve the foreground run, so there is exactly one implementation of
"bring the outputs home and verify them". Two would drift, and the one that drifted would
be the one that lost someone's outputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import ExitCode, GpuOpsError
from .ledger import RunRecord
from .manifest import build_manifest, compare, parse_sha256sum, remote_manifest_script
from .remote import Remote
from .result import read_head_bytes

COLLECT_PLAN_FILE = "collect-plan.json"


@dataclass(frozen=True)
class OutputTarget:
    """One `[[outputs]]` entry, reduced to what collection needs."""
    path: str
    required: bool


@dataclass(frozen=True)
class ReportRequest:
    """The spec's `[report]` table, reduced to what collection needs."""
    files: tuple[str, ...]
    max_bytes: int


@dataclass(frozen=True)
class CollectPlan:
    """Everything needed to collect one run, without its job spec.

    Written to the run directory at launch and read back by `gpu-ops collect`. Frozen,
    because a plan that could be edited after the job started would describe a job that
    is not the one running.
    """
    remote_root: str
    outputs: tuple[OutputTarget, ...]
    report: ReportRequest | None = None

    @classmethod
    def from_spec(cls, spec, remote_root: str) -> "CollectPlan":
        rs = getattr(spec, "report", None)
        return cls(
            remote_root=remote_root,
            outputs=tuple(OutputTarget(o.path, bool(o.required)) for o in spec.outputs),
            report=None if rs is None else ReportRequest(tuple(rs.files), int(rs.max_bytes)),
        )

    # --- on disk ---

    def to_dict(self) -> dict:
        return {
            "remote_root": self.remote_root,
            "outputs": [{"path": o.path, "required": o.required} for o in self.outputs],
            "report": None if self.report is None
            else {"files": list(self.report.files), "max_bytes": self.report.max_bytes},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CollectPlan":
        """Rebuild a plan, or say plainly that it cannot be read.

        A reader tolerates what it did not write only where there is something sensible to
        do; here there is not. Guessing at a half-readable plan would download the wrong
        files, or silently skip an output the caller is waiting for.
        """
        try:
            rep = d.get("report")
            return cls(
                remote_root=str(d["remote_root"]),
                outputs=tuple(OutputTarget(str(o["path"]), bool(o.get("required", True)))
                              for o in d["outputs"]),
                report=None if rep is None
                else ReportRequest(tuple(str(f) for f in rep["files"]), int(rep["max_bytes"])),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise GpuOpsError(ExitCode.INTERNAL, f"collect plan is unreadable: {type(e).__name__}: {e}")

    def save(self, run_dir: Path) -> Path:
        path = run_dir / COLLECT_PLAN_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, run_dir: Path) -> "CollectPlan":
        path = run_dir / COLLECT_PLAN_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"cannot read {path}: {e}")
        except json.JSONDecodeError as e:
            raise GpuOpsError(ExitCode.INTERNAL, f"collect plan is not valid JSON ({path}): {e}")
        return cls.from_dict(raw)


def collect_outputs(remote: Remote, run_dir: Path, plan: CollectPlan, record: RunRecord) -> None:
    """Download the declared outputs, verify them, and fill the record's output fields.

    Fills `outputs_manifest`, `outputs_verified` and `report` on the record *before*
    raising, so a run that failed verification still records what was learned: that is what
    tells the caller the files in `out/` are unverified rather than absent.
    """
    rels = [o.path for o in plan.outputs]
    if not rels:
        record.outputs_verified = True
        read_report(plan.report, run_dir / "out", record)
        return
    out_lines: list[str] = []
    rc = remote.run(remote_manifest_script(plan.remote_root + "/out", rels), timeout_s=900,
                    on_line=out_lines.append)
    if rc != 0:
        raise GpuOpsError(ExitCode.TRANSFER_FAILED, f"could not list outputs on the instance (exit {rc})")
    expected, missing = parse_sha256sum("\n".join(out_lines))
    local_out = run_dir / "out"
    for o in plan.outputs:
        if o.path not in missing:
            remote.get(f"out/{o.path}", run_dir)
    got = build_manifest(local_out, [r for r in rels if r not in missing and (local_out / r).exists()])
    record.outputs_manifest = got
    problems = compare(expected, got)
    record.outputs_verified = not problems
    required_missing = [o.path for o in plan.outputs if o.required and o.path in missing]
    if problems:
        raise GpuOpsError(ExitCode.TRANSFER_FAILED, "output verification failed: " + "; ".join(problems[:5]))
    if required_missing:
        raise GpuOpsError(ExitCode.JOB_FAILED, f"required outputs missing: {required_missing}")
    read_report(plan.report, local_out, record)


def read_report(request: ReportRequest | None, local_out: Path, record: RunRecord) -> None:
    """Include the requested report files in the record, read from the verified local copies.

    Only ever called once the outputs are downloaded and their checksums match, so what
    reaches the result is data gpu-ops has verified.
    """
    if request is None:
        return
    report: dict = {}
    for rel in request.files:
        path = local_out / rel
        if not path.is_file():
            report[rel] = None
            record.notes.append(f"report file missing from the outputs: {rel}")
            continue
        # read the cap, never the whole file: a report file is an output, and outputs
        # can be checkpoints. Secrets are redacted once, per field, by the ledger.
        try:
            raw, size = read_head_bytes(path, request.max_bytes)
        except GpuOpsError as e:
            # a file that vanished or turned unreadable between the check and the read is
            # the missing-optional-output case, not an internal error
            report[rel] = None
            record.notes.append(f"report file could not be read: {rel} ({e.message})")
            continue
        text = raw.decode("utf-8", "replace")
        if size > request.max_bytes:
            report[rel] = {"truncated": True, "bytes": size, "text": text}
            continue
        if path.suffix.lower() == ".json":
            try:
                report[rel] = json.loads(text)
            except json.JSONDecodeError as e:
                report[rel] = {"text": text, "parse_error": str(e)}
            continue
        report[rel] = {"text": text}
    record.report = report


@dataclass(frozen=True)
class Resident:
    """Proof that the job is running on the instance and that someone else could finish it.

    Only `Runner._launch` builds one, and only after the collect plan, the reconnect key and
    the `running` record are all on disk. `run --detach` skips teardown on the strength of
    this object existing — an invariant carried by a type, rather than a boolean a caller
    has to remember to set at exactly the right line.
    """
    instance_id: str
    run_id: str
    pid: int | None
    launched_at: str
    job_deadline_epoch: int


@dataclass(frozen=True)
class RemoteJob:
    """What the instance says about the job, as one answer to one question.

    `phase` is the job's own word for where it is; `alive` is whether its process is still
    there. Both are needed: a job killed by the OOM killer leaves `phase` at `command`
    forever, and only the missing process says so. `died` is that combination, named once
    here rather than re-derived at each caller.
    """
    phase: str  # setup | command | finished | unknown
    started_at: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    finished_at: str | None = None
    alive: bool = False
    last_log_line: str | None = None
    phases: dict[str, float] = field(default_factory=dict)  # seconds per phase, timed on the instance

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    @property
    def died(self) -> bool:
        """Gone without finishing: OOM-killed, or the container restarted under it."""
        return not self.finished and self.pid is not None and not self.alive

    @property
    def reported_phase(self) -> str:
        return "died" if self.died else ("finished" if self.finished else self.phase)


# Printed after the status JSON by `status_script`, one `key=value` per line. Parsed rather
# than merged into the JSON because the job writes that file and this wrapper does not.
_ALIVE = "GPUOPS_ALIVE="
_LASTLOG = "GPUOPS_LASTLOG="


def status_script(root: str) -> str:
    """One short command that answers everything `gpu-ops status` reports.

    Deliberately one round trip: status is the polling path, and a poll that costs three
    SSH connections is a poll nobody can afford to run often.
    """
    return (
        f'R={_q(root)}\n'
        's=$(cat "$R/status.json" 2>/dev/null) || s=""\n'
        '[ -z "$s" ] && s="{}"\n'
        'pid=$(printf "%s" "$s" | sed -n \'s/.*"pid":[ ]*\\([0-9][0-9]*\\).*/\\1/p\')\n'
        'alive=false\n'
        'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive=true; fi\n'
        'printf "%s\\n" "$s"\n'
        f'printf "{_ALIVE}%s\\n" "$alive"\n'
        f'printf "{_LASTLOG}%s\\n" "$(tail -n 1 "$R/job.log" 2>/dev/null | tr -d "\\r")"\n'
    )


def parse_status(lines: list[str]) -> RemoteJob:
    """Read `status_script`'s output. Anything unreadable is `unknown`, never an exception.

    A status read happens while money is being spent, so it must always produce an answer
    the caller can act on: "I could not tell" is a state, not a crash.
    """
    alive = False
    last_log: str | None = None
    json_text: list[str] = []
    for line in lines:
        if line.startswith(_ALIVE):
            alive = line[len(_ALIVE):].strip() == "true"
        elif line.startswith(_LASTLOG):
            last_log = line[len(_LASTLOG):] or None
        else:
            json_text.append(line)
    try:
        d = json.loads("\n".join(json_text).strip() or "{}")
        if not isinstance(d, dict):
            d = {}
    except json.JSONDecodeError:
        d = {}
    pid = d.get("pid")
    exit_code = d.get("exit_code")
    raw_phases = d.get("phases")
    phases = {str(k): float(v) for k, v in raw_phases.items()
              if isinstance(v, (int, float))} if isinstance(raw_phases, dict) else {}
    return RemoteJob(
        phase=str(d.get("phase") or "unknown"),
        started_at=d.get("started_at") or None,
        pid=int(pid) if isinstance(pid, int) else None,
        exit_code=int(exit_code) if isinstance(exit_code, int) else None,
        finished_at=d.get("finished_at") or None,
        alive=alive,
        last_log_line=last_log,
        phases=phases,
    )


def _q(value: str) -> str:
    """Single-quote for sh, so nothing in a path or a secret is ever expanded."""
    return "'" + value.replace("'", "'\\''") + "'"


LAUNCHER = "run.sh"
ENV_FILE = ".job_env"
STATUS_FILE = "status.json"

# The delimiter for every heredoc below. Quoted at the opening, so the shell expands
# nothing inside: a secret containing `$` or a backtick reaches the file unchanged.
_EOF = "GPUOPS_HEREDOC_EOF"


def env_file_script(root: str, exports: dict[str, str]) -> str:
    """Write the job's secret environment to a 0600 file the launcher sources and deletes.

    Secrets reach the instance the same way they always have — over stdin, never in argv —
    but a resident job outlives the SSH session that started it, so they cannot simply be
    that session's environment any more. The file is created under `umask 077`, and the
    launcher removes it the moment it has sourced it: after that the values exist only in
    the running job's environment, as before. The pod is single-tenant and is destroyed at
    teardown.
    """
    body = "".join(f"export {k}={_q(v)}\n" for k, v in exports.items())
    return (f'( umask 077; cat > {_q(root + "/" + ENV_FILE)} <<\'{_EOF}\'\n'
            f'{body}{_EOF}\n)\n')


def phase_script(root: str, name: str, body: str) -> str:
    return (f'( umask 077; cat > {_q(f"{root}/phase-{name}.sh")} <<\'{_EOF}\'\n'
            f'{body}{_EOF}\n)\n')


def launcher_script(root: str, phases: list[str], deadline_epoch: int, run_id: str = "") -> str:
    """The script that owns the job once gpu-ops lets go of it.

    It writes `status.json` as it moves between phases and records the exit code, so a
    later process — or a later machine — can tell what happened without having watched.
    The deadline is an absolute epoch, not a duration, because each phase has to be given
    only the time that is actually left.

    The `nohup … &` must be its own simple command. Written as `cd X && nohup … &` the `&`
    applies to the whole AND-list, so bash forks a subshell that *waits* for the job while
    still holding the caller's stdout pipe — and the launch call then blocks for the entire
    length of the job, which is the one thing a detached launch must never do.
    """
    return (f'( umask 077; cat > {_q(root + "/" + LAUNCHER)} <<\'{_EOF}\'\n'
            f'{_LAUNCHER_BODY}{_EOF}\n)\n'
            f'chmod 700 {_q(root + "/" + LAUNCHER)}\n'
            f'cd {_q(root)}\n'
            f'GPUOPS_ROOT_DIR={_q(root)} GPUOPS_DEADLINE={deadline_epoch} '
            f'GPUOPS_PHASES={_q(" ".join(phases))} GPUOPS_RUN={_q(run_id)} '
            f'nohup setsid bash {_q(root + "/" + LAUNCHER)} '
            f'> {_q(root + "/launcher.log")} 2>&1 < /dev/null &\n'
            'echo started\n')


_LAUNCHER_BODY = r"""set -u
R="$GPUOPS_ROOT_DIR"
LOG="$R/job.log"
STATUS="$R/status.json"
DEADLINE="$GPUOPS_DEADLINE"
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
MAINPID=$$

PHASES_JSON=""

# The instance times what happens on the instance. Deriving these locally from polling
# missed any phase that started and ended between two polls, which is most of them for a
# short job — and `spend`'s overhead share is computed from exactly these numbers.
add_phase() {
  PHASES_JSON="$PHASES_JSON${PHASES_JSON:+,}\"$1\":$2"
}

write_status() {  # phase, exit_code (number or null), finished_at (quoted string or null)
  tmp="$STATUS.tmp"
  printf '{"phase":"%s","started_at":"%s","pid":%s,"exit_code":%s,"finished_at":%s,"phases":{%s}}\n' \
    "$1" "$STARTED" "$MAINPID" "$2" "$3" "$PHASES_JSON" > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATUS"
}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

budget() {  # seconds left before the collection window opens; never zero
  b=$(( DEADLINE - $(date +%s) ))
  [ "$b" -lt 1 ] && b=1
  printf '%s' "$b"
}

finish() {  # exit code of the job
  write_status finished "$1" "\"$(now)\""
  exit 0
}

# The exports live in a 0600 file only until they are in this process's environment.
if [ -f "$R/.job_env" ]; then
  . "$R/.job_env"
  rm -f "$R/.job_env"
fi

# Set here, not in a prelude each phase script has to remember to carry: a phase that ran
# without $GPUOPS_OUT would write the job's results to / and report success.
export GPUOPS_ROOT="$R" GPUOPS_IN="$R/in" GPUOPS_OUT="$R/out" GPUOPS_WORK="$R/work"
export GPUOPS_RUN_ID="$GPUOPS_RUN"
mkdir -p "$GPUOPS_WORK" "$GPUOPS_OUT"
cd "$GPUOPS_WORK"

write_status starting null null

for phase in $GPUOPS_PHASES; do
  printf '[gpu-ops] phase: %s\n' "$phase" >> "$LOG"
  write_status "$phase" null null
  t0=$(date +%s)
  timeout -k 30 "$(budget)" bash "$R/phase-$phase.sh" >> "$LOG" 2>&1
  rc=$?
  add_phase "$phase" "$(( $(date +%s) - t0 ))"
  if [ "$rc" -ne 0 ]; then
    finish "$rc"
  fi
done
finish 0
"""


def finalize(record: RunRecord, *, started: datetime, ended: datetime, price_per_hr: float,
             teardown_ok: bool) -> None:
    """Close a record out once the machine is gone (or gave up being gone).

    One implementation, because the foreground run and `collect` both end here and the
    numbers they write feed `spend`: two copies would price the same run two ways.
    """
    record.ended_at = ended.isoformat()
    record.teardown_confirmed = teardown_ok
    secs = max(0.0, (ended - started).total_seconds())
    record.billed_seconds = round(secs, 1)
    record.cost_estimate_usd = round((price_per_hr or 0) * secs / 3600, 4)
