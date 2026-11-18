"""Reattaching to a detached run: `gpu-ops status`, `wait` and `collect`.

The process that launched a detached run is gone, so everything here is rebuilt from the
run directory — the record, the collect plan and the reconnect key — plus the provider,
which is the only thing that can say whether the machine is still alive.

Two safety rules shape the whole module:

- **The machine is the provider's business, never the ledger's.** Terminating needs only
  the instance id and the API; it never needs SSH, the key or the plan. So a run whose
  outputs cannot be fetched can still always be shut down, and `collect` shuts it down.
- **Nothing is collected twice and nothing is collected too late.** `collect` is
  idempotent, and once the deadline has passed it says the outputs are gone rather than
  pretending to look for them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import ExitCode, GpuOpsError, worst
from .ledger import Ledger, RunRecord
from .providers.base import Instance, Provider
from .remote import Remote
from .resident import CollectPlan, RemoteJob, collect_outputs, finalize, parse_status, status_script
from .runner import RunOptions, teardown

Log = Callable[[str], None]


def _utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Detached:
    """One detached run, reattached from its run directory.

    Built by `load`, which is the only constructor: it is what proves the record, the plan
    and the key are all actually there before anything tries to act on the run.
    """
    ledger: Ledger
    run_id: str
    run_dir: Path
    record_path: Path
    raw: dict  # the record as it is on disk, so unknown keys survive a patch
    plan: CollectPlan
    provider: Provider
    key_path: Path
    opts: RunOptions
    log: Log

    # ---------- construction ----------

    @classmethod
    def load(cls, ledger: Ledger, run_id: str, providers: dict[str, Provider], *,
             opts: RunOptions | None = None, log: Log | None = None) -> "Detached":
        import json
        run_dir = ledger.run_dir(run_id)  # validates the id; never a path outside runs/
        record_path = run_dir / "record.json"
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
        except OSError:
            raise GpuOpsError(ExitCode.SPEC_INVALID, f"no run {run_id!r} here ({record_path} does not exist)")
        except json.JSONDecodeError as e:
            raise GpuOpsError(ExitCode.INTERNAL, f"run record is not valid JSON ({record_path}): {e}")
        name = raw.get("provider") or ""
        provider = providers.get(name)
        if provider is None:
            raise GpuOpsError(ExitCode.REFUSED,
                              f"run {run_id} was created on provider {name!r}, which is not configured "
                              f"or not allowed by policy (configured: {sorted(providers) or 'none'})")
        return cls(ledger=ledger, run_id=run_id, run_dir=run_dir, record_path=record_path, raw=raw,
                   plan=CollectPlan.load(run_dir), provider=provider,
                   key_path=run_dir / "key" / "id_ed25519", opts=opts or RunOptions(),
                   log=log or (lambda _s: None))

    # ---------- the run's own facts ----------

    @property
    def instance_id(self) -> str | None:
        return self.raw.get("instance_id")

    @property
    def started(self) -> datetime | None:
        return _parse_time(self.raw.get("started_at"))

    @property
    def deadline(self) -> datetime | None:
        """When the machine dies, whatever anyone does: the dead-man timer and the reaper
        both work from the deadline in its name."""
        return _parse_time(self.raw.get("deadline"))

    @property
    def collected(self) -> bool:
        """Already finished: a terminal status, or an end time written by a release."""
        return bool(self.raw.get("ended_at")) or self.raw.get("status") in ("ok", "failed")

    def minutes_to_deadline(self) -> float | None:
        d = self.deadline
        return None if d is None else round((d - _utc()).total_seconds() / 60, 1)

    def cost_so_far_usd(self) -> float | None:
        started, price = self.started, self.raw.get("price_per_hr")
        if started is None or not price:
            return None
        return round(price * (_utc() - started).total_seconds() / 3600, 4)

    # ---------- talking to the machine ----------

    def instance(self) -> Instance | None:
        if not self.instance_id:
            return None
        try:
            return self.provider.get(self.instance_id)
        except GpuOpsError:
            raise
        except Exception as e:
            raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"could not ask the provider about the instance: {e}")

    def remote(self, inst: Instance) -> Remote:
        if not self.key_path.is_file():
            raise GpuOpsError(ExitCode.TRANSFER_FAILED,
                              f"the reconnect key for run {self.run_id} is gone ({self.key_path}), so the "
                              f"outputs cannot be fetched. `gpu-ops kill {inst.id}` still stops the spending.")
        remote = self.provider.remote_for(inst, self.key_path)
        if remote is None:
            raise GpuOpsError(ExitCode.TRANSFER_FAILED,
                              f"the instance for run {self.run_id} has no direct SSH endpoint any more, so the "
                              f"outputs cannot be fetched. `gpu-ops kill {inst.id}` still stops the spending.")
        return remote

    def job(self, remote: Remote) -> RemoteJob:
        lines: list[str] = []
        remote.run(status_script(self.plan.remote_root), timeout_s=120, on_line=lines.append)
        return parse_status(lines)

    # ---------- status ----------

    def status(self) -> dict:
        """What the run is doing, cheaply. Reports; it never changes anything.

        Every key is always present, like the compact run result: a caller that has to guard
        each lookup is a caller that breaks.
        """
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.raw.get("status"),
            "phase": None,
            "instance_id": self.instance_id,
            "collected": self.collected,
            "elapsed_min": None,
            "minutes_to_deadline": self.minutes_to_deadline(),
            "collect_by": _iso(self.collect_by()),
            "cost_so_far_usd": self.cost_so_far_usd(),
            "job_exit_code": None,
            "last_log_line": None,
            "error": self.raw.get("error"),
        }
        started = self.started
        if started is not None:
            out["elapsed_min"] = round((_utc() - started).total_seconds() / 60, 1)
        if self.collected:
            out["phase"] = "collected"
            out["job_exit_code"] = self.raw.get("exit_code")
            out["cost_so_far_usd"] = self.raw.get("cost_estimate_usd")
            return out
        inst = self.instance()
        if inst is None or not inst.alive:
            # the machine is gone and nothing collected: the dead-man timer or the reaper got
            # there first, and the outputs went with it
            out["phase"] = "gone"
            return out
        job = self.job(self.remote(inst))
        out["phase"] = job.reported_phase
        out["job_exit_code"] = job.exit_code
        out["last_log_line"] = job.last_log_line
        return out

    def collect_by(self) -> datetime | None:
        """The end of the collection window, as the launching process recorded it."""
        for note in self.raw.get("notes") or []:
            if note.startswith("job runs on the instance; collect by "):
                return _parse_time(note.rsplit(" ", 1)[-1])
        return self.deadline

    # ---------- collect ----------

    def collect(self) -> tuple[ExitCode, dict]:
        """Bring the outputs home if the job has finished, then shut the machine down.

        Idempotent: a run that was already collected is reported as it stands and nothing
        is touched. A run still working is left strictly alone — same machine, same job.
        """
        if self.collected:
            code = ExitCode(self.raw.get("exit_code") or 0)
            self.log(f"run {self.run_id} was already collected")
            return code, self.raw
        inst = self.instance()
        if inst is None or not inst.alive:
            return self._outputs_lost(), self.raw
        remote = self.remote(inst)
        job = self.job(remote)
        if not (job.finished or job.died):
            if self._past_window():
                return self._stop_and_lose(inst, "the collection window closed while the job was still running"), self.raw
            self.log(f"run {self.run_id} is still in phase {job.reported_phase}; nothing collected")
            return ExitCode.STILL_RUNNING, self.raw
        return self._finish(inst, remote, job), self.raw

    def _past_window(self) -> bool:
        by = self.collect_by()
        return by is not None and _utc() >= by

    def _finish(self, inst: Instance, remote: Remote, job: RemoteJob) -> ExitCode:
        """The job has ended: fetch the log and the outputs, then release the machine."""
        record = RunRecord.from_dict(self.raw)
        record.phases.update(job.phases)
        code = ExitCode.OK
        self._fetch_log(remote)
        try:
            if job.died:
                raise GpuOpsError(ExitCode.JOB_FAILED,
                                  "the job's process disappeared without finishing (killed on the instance, "
                                  "e.g. out of memory); see `gpu-ops logs`")
            if job.exit_code == 124:  # coreutils timeout: the job hit its deadline
                raise GpuOpsError(ExitCode.CAP_HIT, "the job hit its deadline and was killed")
            if job.exit_code != 0:
                raise GpuOpsError(ExitCode.JOB_FAILED,
                                  f"the job exited with code {job.exit_code} (see `gpu-ops logs`)")
            collect_outputs(remote, self.run_dir, self.plan, record)
        except GpuOpsError as e:
            code, record.error = e.code, e.message
        except Exception as e:
            code, record.error = ExitCode.TRANSFER_FAILED, f"collect failed: {e!r}"
        return self._release(inst, record, code)

    def _release(self, inst: Instance, record: RunRecord, outcome: ExitCode = ExitCode.OK) -> ExitCode:
        """Terminate, confirm, price the run, and write it all down once.

        `outcome` is what the job itself did, and it is folded in here rather than by the
        caller: `_save` happens inside this method, so a caller that combined the codes
        afterwards would leave a record saying `exit_code: 0` for a job that failed — the
        record and the exit code must never be able to disagree.
        """
        ok = False
        try:
            ok = teardown(self.provider, inst.id, self.opts.teardown_timeout_s, self.opts.poll_s)
        except Exception as e:
            record.notes.append(f"teardown error: {e!r}")
        finalize(record, started=self.started or _utc(), ended=_utc(),
                 price_per_hr=record.price_per_hr or 0, teardown_ok=ok)
        code = worst(outcome, ExitCode.OK if ok else ExitCode.TEARDOWN_FAILED)
        if not ok:
            self.log(f"TEARDOWN FAILED — INSTANCE MAY STILL BE BILLING: {self.provider.name} {inst.id}")
        else:
            self.log(f"terminated {inst.id}; est. cost ${record.cost_estimate_usd}")
        self._save(record, code)
        self.ledger.event(self.run_id, "terminated" if ok else "live", instance_id=inst.id)
        self._drop_key()
        return code

    def _outputs_lost(self) -> ExitCode:
        """The machine is already gone and nothing was collected from it."""
        record = RunRecord.from_dict(self.raw)
        record.outputs_verified = False
        record.error = ("the instance was deleted before the outputs were collected, so they are lost; "
                        "the dead-man timer or the reaper removed it at the deadline")
        record.notes.append("outputs lost: the machine was gone before `collect` ran")
        finalize(record, started=self.started or _utc(), ended=_utc(),
                 price_per_hr=record.price_per_hr or 0, teardown_ok=True)
        self.log(f"run {self.run_id}: the instance is already gone; the outputs were lost")
        self._save(record, ExitCode.TRANSFER_FAILED)
        self.ledger.event(self.run_id, "terminated", instance_id=self.instance_id)
        self._drop_key()
        return ExitCode.TRANSFER_FAILED

    def _stop_and_lose(self, inst: Instance, why: str) -> ExitCode:
        """Past the window with the job still going: stop the spending, say the outputs are lost."""
        record = RunRecord.from_dict(self.raw)
        record.outputs_verified = False
        record.error = f"{why}; the outputs were not collected"
        self.log(f"run {self.run_id}: {why}; shutting the machine down")
        return self._release(inst, record, ExitCode.TRANSFER_FAILED)

    def fetch_log(self) -> bool:
        """Bring the instance's job.log home mid-run, for `gpu-ops logs` on a detached run.

        A detached run writes its log on the instance, so until it is collected there is
        nothing local to read. Returns False when the machine is gone, which is the caller's
        cue that the log went with it.
        """
        inst = self.instance()
        if inst is None or not inst.alive:
            return False
        self._fetch_log(self.remote(inst))
        return (self.run_dir / "job.log").is_file()

    def _fetch_log(self, remote: Remote) -> None:
        import shlex
        lines: list[str] = []
        try:
            remote.run(f"cat {shlex.quote(self.plan.remote_root)}/job.log 2>/dev/null || true\n",
                       timeout_s=300, on_line=lines.append)
        except Exception:
            return  # a log we could not fetch must not hide the run's own outcome
        if lines:
            redact = self.ledger.redactor
            text = "".join((redact(x) if redact else x) + "\n" for x in lines)
            (self.run_dir / "job.log").write_text(text, encoding="utf-8")

    def _drop_key(self) -> None:
        """The machine is gone, so the key that reached it is of no further use."""
        try:
            if self.key_path.is_file():
                self.key_path.unlink()
            pub = self.key_path.with_suffix(".pub")
            if pub.is_file():
                pub.unlink()
        except OSError:
            pass

    def _save(self, record: RunRecord, code: ExitCode) -> None:
        """Write back only the fields this process decided, so keys it does not know survive."""
        record.exit_code = int(code)
        record.status = "ok" if code == ExitCode.OK else "failed"
        changed = {k: getattr(record, k) for k in (
            "status", "exit_code", "error", "ended_at", "billed_seconds", "cost_estimate_usd",
            "teardown_confirmed", "outputs_manifest", "outputs_verified", "report", "notes", "phases")}
        self.raw = {**self.raw, **changed}
        self.ledger.patch_record(self.record_path, **changed)

    # ---------- wait ----------

    def wait(self, timeout_s: float | None, poll_s: float = 15) -> tuple[ExitCode, dict]:
        """Block until the job ends, then collect. Prints nothing; returns one result.

        Bounded by the collection window as well as by `timeout_s`: waiting past it would
        mean watching the deadline delete the outputs.
        """
        end = None if not timeout_s else time.monotonic() + timeout_s
        while True:
            code, res = self.collect()
            if code != ExitCode.STILL_RUNNING:
                return code, res
            if end is not None and time.monotonic() >= end:
                self.log(f"run {self.run_id} is still running; waited {timeout_s:.0f}s")
                return ExitCode.STILL_RUNNING, self.raw
            by = self.collect_by()
            left = poll_s if by is None else min(poll_s, max(1.0, (by - _utc()).total_seconds()))
            time.sleep(left)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
