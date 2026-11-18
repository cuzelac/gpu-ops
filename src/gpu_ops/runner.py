"""The run lifecycle: acquire → execute → release, with teardown guaranteed.

Every path out of `Runner.run()` — success, job failure, exception, Ctrl-C, SIGTERM —
goes through `_release()`, which deletes the instance and confirms it is gone.
Kept as three parts so sessions (acquire once, execute many, release once) can be added
later without a rewrite.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from .errors import ExitCode, GpuOpsError, worst
from .ledger import Ledger, RunRecord
from .manifest import (build_manifest, compare, manifest_digest, parse_sha256sum, remote_manifest_script,
                       sha256_file)
from .names import instance_name, new_run_id
from .planner import Plan
from .policy import Policy
from .providers.base import CreateRequest, Instance, Provider
from .remote import TIMEOUT_EXIT, Remote, TransferError
from .resident import (CollectPlan, RemoteJob, Resident, collect_outputs, env_file_script, finalize,
                       launcher_script, parse_status, phase_script, status_script)
from .secrets import Redactor
from .spec import LoadedSpec

RemoteFactory = Callable[[Instance, Path], "Remote | None"]
Log = Callable[[str], None]

# how long before the deadline the job is stopped, so collection and teardown still fit
DEFAULT_COLLECT_WINDOW_S = 300.0


def _noop_log(_line: str) -> None:
    pass

BOOTSTRAP = r"""
set -e
if ! command -v sshd >/dev/null 2>&1 || ! command -v rsync >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq openssh-server rsync curl ca-certificates >/dev/null
fi
mkdir -p /root/.ssh /run/sshd {root}/in {root}/out {root}/work
chmod 700 /root/.ssh
# SSH sessions don't inherit this process's environment; keep the provider's injected variables
# (e.g. RUNPOD_POD_ID / RUNPOD_API_KEY) where the dead-man scripts can source them
( umask 077; export -p | grep -E '^declare -x (RUNPOD_|VAST_|CONTAINER_)' > {root}/.provider_env || true )
printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
ssh-keygen -A >/dev/null 2>&1 || true
/usr/sbin/sshd -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no
exec sleep infinity
"""


@dataclass
class RunOptions:
    ready_timeout_s: float = 600
    teardown_timeout_s: float = 300
    poll_s: float = 5
    remote_root: str = "/gpuops"


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _dq(value: str) -> str:
    """Double-quote for bash, keeping $VAR expansion (env values may use $GPUOPS_IN)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'


class Runner:
    def __init__(self, loaded: LoadedSpec, plan: Plan, policy: Policy, provider: Provider, ledger: Ledger,
                 remote_factory: RemoteFactory, redactor: Redactor, secrets: dict[str, str],
                 opts: RunOptions | None = None, log: Log | None = None, job_log: Log | None = None):
        self.loaded, self.spec, self.plan, self.policy = loaded, loaded.spec, plan, policy
        self.provider, self.ledger, self.remote_factory = provider, ledger, remote_factory
        self.redact, self.secrets = redactor, secrets
        self.opts = opts or RunOptions()
        self.log = log or _noop_log  # phase lines
        self.job_log = job_log or _noop_log  # echo of the job's output; job.log is written either way
        self._echo_log = job_log is not None  # only then is it worth fetching the log while polling
        self._log_echoed = 0  # lines of the instance's job.log already echoed
        self.resident: Resident | None = None  # set once the job is running and recoverable
        self.handed_off: Resident | None = None  # set ONLY by --detach; skips teardown
        self.collect_window_s = DEFAULT_COLLECT_WINDOW_S  # replaced per run by _collect_window
        self._detach = False  # what _apply_plan sizes the collection window for
        self.inputs_manifest: dict[str, str] = {}  # kept off the record: see ledger's module docstring
        if ledger.redactor is None:  # the runner is the only writer holding secrets
            ledger.redactor = redactor

        self.run_id = new_run_id()
        self.started = _utc()
        self.instance: Instance | None = None
        self.remote: Remote | None = None
        self.key_dir: Path | None = None
        self.run_dir = ledger.ensure_run_dir(self.run_id)
        self.record = RunRecord(run_id=self.run_id, job=self.spec.name, project=self.spec.project,
                                kind=self.spec.kind, spec_path=str(loaded.path), image=self.spec.image.ref)
        self._apply_plan(plan)

    MAX_CREATE_ATTEMPTS = 3

    def _apply_plan(self, plan: Plan) -> None:
        """(Re)derive everything that depends on the chosen offer. Used at start and on capacity fallback."""
        self.plan = plan
        self.deadline = self.started + timedelta(minutes=plan.run_minutes)
        self.name = instance_name(self.run_id, int(self.deadline.timestamp()), self.spec.name)
        run_s = plan.run_minutes * 60
        # time kept back after the command for download + teardown, so they happen before the deadline
        self.reserve_s = min(300.0, max(60.0, 0.1 * run_s), 0.5 * run_s)
        o, r = plan.offer, self.record
        r.provider, r.instance_name, r.gpu_id, r.gpu_name = o.provider, self.name, o.gpu_id, o.gpu_name
        r.gpu_count, r.vram_gb, r.tier, r.price_per_hr = o.count, o.vram_gb, o.tier, o.price_per_hr
        r.worst_case_usd, r.deadline = round(plan.worst_case_usd, 4), self.deadline.isoformat()
        r.notes = list(dict.fromkeys([*r.notes, *plan.notes]))
        # recomputed here too: a capacity fallback re-derives the deadline, and a window
        # sized against the old one would leave the job the wrong amount of time
        self.collect_window_s = self._collect_window(self._detach)

    # ---------- helpers ----------

    def _remaining(self, reserve: bool = False) -> float:
        left = (self.deadline - _utc()).total_seconds()
        return left - self.reserve_s if reserve else left

    @contextmanager
    def _phase(self, name: str) -> Iterator[None]:
        t0 = time.monotonic()
        self.log(f"[{name}] …")
        try:
            yield
        finally:
            self.record.phases[name] = round(self.record.phases.get(name, 0) + time.monotonic() - t0, 2)

    def _write_log(self, line: str) -> None:
        with (self.run_dir / "job.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _job_log(self, line: str) -> None:
        line = self.redact(line)
        self._write_log(line)
        self.job_log(line)

    def _phase_body(self, lines: list[str]) -> str:
        """One phase's script. `GPUOPS_*` and the secrets come from the launcher's own
        environment, so a phase cannot run without them."""
        head = ["set -euo pipefail"]
        head += [f"export {k}={_dq(v)}" for k, v in self.spec.env.items()]
        head.append('cd "$GPUOPS_WORK"')
        return "\n".join(head) + "\n" + "\n".join(lines) + "\n"

    def _make_key(self) -> str:
        import os
        if self._detach:
            # A detached run is reconnected to by a later process, possibly after the PC has
            # slept or rebooted — which is the whole point. $XDG_RUNTIME_DIR and /dev/shm are
            # memory-backed and do not survive that, so the key lives beside the run and is
            # deleted at release, where a foreground run's key already was.
            self.key_dir = self.run_dir / "key"
            self.key_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.key_dir, 0o700)
        else:
            base = os.environ.get("XDG_RUNTIME_DIR") or ("/dev/shm" if Path("/dev/shm").is_dir() else None)
            self.key_dir = Path(tempfile.mkdtemp(prefix="gpuops-key-", dir=base))
        key = self.key_dir / "id_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"gpuops-{self.run_id}", "-f", str(key)],
                       check=True, capture_output=True)
        if self._detach:
            os.chmod(key, 0o600)
            # a state directory on a Windows drive cannot hold the mode; say so rather than
            # implying a protection that is not there
            if (key.stat().st_mode & 0o077) != 0:
                self.record.notes.append(
                    f"the reconnect key at {key} is not private to you (mode "
                    f"{key.stat().st_mode & 0o777:o}); the state directory's filesystem cannot hold it")
        return (self.key_dir / "id_ed25519.pub").read_text().strip()

    def key_path(self) -> Path | None:
        return None if self.key_dir is None else self.key_dir / "id_ed25519"

    # ---------- lifecycle ----------

    def _collect_window(self, detach: bool) -> float:
        """How much time to keep back after the job for collection and teardown.

        A foreground run collects the moment the job ends, so today's reserve is enough. A
        detached one is collected whenever its caller comes back, and nothing between the
        job finishing and the deadline will save the outputs — the dead-man timer deletes
        the machine and everything on it. Never more than half the run, so asking for a
        generous window cannot leave no time to do the work.
        """
        if not detach:
            return self.reserve_s
        want = self.policy.limits.detached_collect_minutes * 60
        return min(max(self.reserve_s, want), 0.5 * self.plan.run_minutes * 60)

    def run(self, *, detach: bool = False) -> tuple[ExitCode, RunRecord]:
        code = ExitCode.OK
        self.record.started_at = self.started.isoformat()
        self._detach = detach
        self.collect_window_s = self._collect_window(detach)
        self.ledger.event(self.run_id, "creating", provider=self.provider.name, name=self.name,
                          deadline=self.deadline.isoformat(), job=self.spec.name, project=self.spec.project)
        try:
            self._hash_inputs()
            self._acquire()
            if detach:
                self._upload()
                # from here the machine is deliberately left alive. Assigning `handed_off`
                # is the single line that skips teardown, and it can only be given a
                # `Resident`, which `_launch` makes only once the plan, the key and the
                # record are on disk — so the skip cannot happen before the run is
                # recoverable by someone else.
                self.resident = self.handed_off = self._launch()
                self.log(f"detached: run {self.run_id} is running on {self.record.instance_id}; "
                         f"collect by {self.job_deadline():%H:%M:%S} UTC")
                return ExitCode.OK, self.record
            self._execute()
        except GpuOpsError as e:
            code, self.record.error = e.code, e.message
        except TransferError as e:
            code, self.record.error = ExitCode.TRANSFER_FAILED, str(e)
        except (KeyboardInterrupt, SystemExit) as e:
            code, self.record.error = ExitCode.JOB_FAILED, f"interrupted ({type(e).__name__}); tearing down"
        except Exception as e:  # a bug in gpu-ops: still tear down, and say so
            code, self.record.error = ExitCode.INTERNAL, f"internal error: {e!r}"
        finally:
            # `handed_off`, not `resident`: a foreground run is resident too, and must still
            # be torn down here. Only the detach branch ever hands the machine over.
            if self.handed_off is None:
                self._fetch_job_log()
                code = worst(code, self._release())
        self.record.exit_code = int(code)
        self.record.status = "ok" if code == ExitCode.OK else "failed"
        if self.spec.report is not None and self.record.report is None:
            self.record.notes.append("report omitted: the outputs were not downloaded and verified")
        # the ledger is the single applier of the field policy; take back what it wrote so
        # the record this returns is the redacted one, without a second scrub here
        self.record = self.ledger.write_record(self.record)
        return code, self.record

    def _hash_inputs(self) -> None:
        full: dict[str, str] = {}
        for inp in self.spec.inputs:
            local = self.loaded.input_paths[inp.name]
            if local.is_file():
                m = {local.name: sha256_file(local)}
            else:
                m = build_manifest(local, inp.include or None)
            full.update({f"{inp.name}/{k}": v for k, v in m.items()})
        self.inputs_manifest = full
        self.record.inputs_hash = manifest_digest(full)
        self.record.inputs_file_count = len(full)
        # scrub_tree, not scrub: here the *keys* are the file paths, and the values are
        # sha256 hashes that can never be a secret
        (self.run_dir / "inputs-manifest.json").write_text(
            json.dumps(self.redact.scrub_tree(full), indent=2, sort_keys=True), encoding="utf-8")

    def _acquire(self) -> None:
        with self._phase("provision"):
            pub = self._make_key()
            self.instance = self._create_with_fallback(pub)
            self.record.instance_id = self.instance.id
            # written now, not only at the end: a run killed with kill -9 must still show up in `spend`
            self.ledger.write_record(self.record)
            self.ledger.event(self.run_id, "created", instance_id=self.instance.id)
            self.log(f"created {self.instance.id} ({self.plan.offer.gpu_name} {self.plan.offer.tier}, "
                     f"${self.plan.offer.price_per_hr}/hr), deadline {self.deadline:%H:%M:%S} UTC")
            self._wait_running()
        with self._phase("bootstrap"):
            assert self.remote is not None
            if not self.remote.wait_ready(max(10, min(self.opts.ready_timeout_s, self._remaining(True)))):
                raise GpuOpsError(ExitCode.PROVIDER_ERROR, "instance never became reachable over SSH")
            self._arm_deadman()

    def _create_with_fallback(self, public_key: str) -> Instance:
        """Create on the planned offer; if the provider has no capacity, try the next-cheapest alternatives.

        Each alternative is re-planned against the spec's caps and the policy, so the run never exceeds
        max_cost_usd; a pricier GPU just gets an earlier deadline. Only capacity errors fall back.
        """
        alternatives = list(self.plan.alternatives)
        attempts = 0
        while True:
            attempts += 1
            req = CreateRequest(name=self.name, offer=self.plan.offer, image=self.spec.image.ref,
                                bootstrap=BOOTSTRAP.replace("{root}", self.opts.remote_root), public_key=public_key,
                                disk_gb=self.spec.run.disk_gb)
            try:
                return self.provider.create(req)
            except GpuOpsError as e:
                if e.code != ExitCode.NO_OFFER or attempts >= self.MAX_CREATE_ATTEMPTS:
                    raise
                failed = self.plan.offer
                nxt = self._next_plan(alternatives)
                if nxt is None:
                    raise
                self.record.notes.append(f"no capacity for {failed.gpu_name} ({failed.tier}, ${failed.price_per_hr}/hr);"
                                         f" fell back to {nxt.offer.gpu_name} ({nxt.offer.tier}, "
                                         f"${nxt.offer.price_per_hr}/hr)")
                self.log(self.record.notes[-1])
                self._apply_plan(nxt)
                self.ledger.event(self.run_id, "creating", provider=self.provider.name, name=self.name,
                                  deadline=self.deadline.isoformat(), job=self.spec.name, project=self.spec.project)
            except Exception as e:
                raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"create failed: {e}")

    def _next_plan(self, alternatives: list) -> Plan | None:
        from .planner import plan_for_offer
        while alternatives:
            offer = alternatives.pop(0)
            if offer.provider != self.provider.name:
                continue
            try:
                p = plan_for_offer(self.spec, self.policy, offer, self.plan.month_spent_usd)
            except GpuOpsError:
                continue
            p.alternatives = alternatives
            return p
        return None

    def _wait_running(self) -> None:
        assert self.instance is not None and self.key_dir is not None
        end = time.monotonic() + min(self.opts.ready_timeout_s, max(10, self._remaining(True)))
        while True:
            inst = self.provider.get(self.instance.id)
            if inst is None or inst.status in ("terminated", "error", "exited"):
                raise GpuOpsError(ExitCode.PROVIDER_ERROR,
                                  f"instance stopped while starting (status {inst.status if inst else 'gone'})")
            self.instance = inst
            if inst.status == "running":
                self.record.data_center = inst.data_center
                if inst.price_per_hr:
                    self.record.price_per_hr = inst.price_per_hr
                remote = self.remote_factory(inst, self.key_dir / "id_ed25519")
                if remote is not None:
                    self.remote = remote
                    return
            if time.monotonic() > end:
                why = "no direct SSH port was assigned" if inst.status == "running" else f"status {inst.status}"
                raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"instance not ready in time ({why})")
            time.sleep(self.opts.poll_s)

    def _arm_deadman(self) -> None:
        assert self.remote is not None
        st = self.provider.self_terminate()
        armed = False
        root = self.remote.root
        load_env = f"[ -f {root}/.provider_env ] && . {root}/.provider_env\n"
        if st is not None and self.remote.run(load_env + st.check, timeout_s=60) == 0:
            secs = max(1, int(self._remaining()))
            script = (f"cat > {root}/deadman.sh <<'GPUOPS_EOF'\n{load_env}{st.kill}\nGPUOPS_EOF\n"
                      f"nohup setsid bash -c 'sleep {secs}; bash {root}/deadman.sh' "
                      f"> {root}/deadman.log 2>&1 < /dev/null &\n")
            armed = self.remote.run(script, timeout_s=60) == 0
        elif st is not None and st.diagnose:
            lines: list[str] = []
            self.remote.run(load_env + st.diagnose, timeout_s=60, on_line=lines.append)
            self.record.notes.append("dead-man diagnosis: " + self.redact(" ".join(lines))[:400])
        self.record.deadman = "armed" if armed else "unarmed"
        self.log(f"dead-man timer {'armed' if armed else 'NOT armed'}")
        limit = self.policy.limits.allow_unarmed_deadman_minutes
        if not armed and self.plan.run_minutes > limit:
            raise GpuOpsError(ExitCode.REFUSED, f"dead-man timer could not be armed and the run is longer than the "
                                                f"policy's allow_unarmed_deadman_minutes ({limit})")

    def _execute(self) -> None:
        self._upload()
        self.resident = self._launch()
        self._await_job()
        with self._phase("download"):
            self._collect()

    def _upload(self) -> None:
        assert self.remote is not None
        remote = self.remote
        with self._phase("upload"):
            for inp in self.spec.inputs:
                remote.put(self.loaded.input_paths[inp.name], f"in/{inp.name}", inp.include or None)
            if self.inputs_manifest:
                out: list[str] = []
                rels = sorted({k.split("/", 1)[0] for k in self.inputs_manifest})
                remote.run(remote_manifest_script(remote.root + "/in", rels), timeout_s=600, on_line=out.append)
                got, _ = parse_sha256sum("\n".join(out))
                problems = compare(self.inputs_manifest, got, missing="did not arrive on the machine")
                if problems:
                    raise GpuOpsError(ExitCode.TRANSFER_FAILED, "upload verification failed: " + "; ".join(problems[:5]))

    def job_deadline(self) -> datetime:
        """When the job itself must stop, leaving the collection window before the deadline.

        The instance dies at `self.deadline` — the dead-man timer and the reaper both work
        from it — so everything that has to happen *after* the job has to fit before it.
        """
        return self.deadline - timedelta(seconds=self.collect_window_s)

    def _launch(self) -> Resident:
        """Start the job on the instance and make the run recoverable without this process.

        The order matters and is the whole safety argument for `--detach`: the collect plan,
        the reconnect key and the `running` record are all on disk *before* a `Resident`
        exists, so a `Resident` is proof that someone else can finish this run.
        """
        assert self.remote is not None and self.instance is not None
        remote = self.remote
        with self._phase("launch"):
            budget = (self.job_deadline() - _utc()).total_seconds()
            if budget <= 0:
                raise GpuOpsError(ExitCode.CAP_HIT, "no time left to run the job before its collection window")
            phases = [p for p, lines in (("setup", self.spec.run.setup),
                                         ("command", [self.spec.run.command])) if lines]
            if self.secrets:
                if remote.run(env_file_script(remote.root, self.secrets), timeout_s=120) != 0:
                    raise GpuOpsError(ExitCode.PROVIDER_ERROR, "could not write the job environment")
            for phase in phases:
                lines = self.spec.run.setup if phase == "setup" else [self.spec.run.command]
                if remote.run(phase_script(remote.root, phase, self._phase_body(lines)), timeout_s=120) != 0:
                    raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"could not write the {phase} script")
            self.collect_plan().save(self.run_dir)
            started = _utc()
            rc = remote.run(launcher_script(remote.root, phases, int(self.job_deadline().timestamp()),
                                            self.run_id), timeout_s=120)
            if rc != 0:
                raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"could not start the job on the instance (exit {rc})")
            job = self._job_status()
            self.record.status = "running"
            self.record.notes.append(f"job runs on the instance; collect by {self.job_deadline().isoformat()}")
            self.ledger.event(self.run_id, "running", instance_id=self.instance.id, pid=job.pid)
            # take back the redacted copy the ledger wrote, so the record this run carries
            # from here on is the one on disk
            self.record = self.ledger.write_record(self.record)
            return Resident(instance_id=self.instance.id, run_id=self.run_id, pid=job.pid,
                            launched_at=started.isoformat(),
                            job_deadline_epoch=int(self.job_deadline().timestamp()))

    def _job_status(self) -> RemoteJob:
        assert self.remote is not None
        lines: list[str] = []
        self.remote.run(status_script(self.remote.root), timeout_s=120, on_line=lines.append)
        return parse_status(lines)

    def _await_job(self) -> None:
        """Poll the resident job until it ends, timing each phase as it goes past.

        Polling, not streaming: the job's output goes to `job.log` on the instance and is
        brought back once, so a thousand progress lines cost one download instead of a
        thousand lines of someone's context (#2).
        """
        while True:
            job = self._job_status()
            self._pump_job_log()
            if job.finished or job.died:
                self.record.phases.update(job.phases)
            if job.finished:
                if job.exit_code == TIMEOUT_EXIT:
                    raise GpuOpsError(ExitCode.CAP_HIT, "the job hit its deadline and was killed")
                if job.exit_code != 0:
                    raise GpuOpsError(ExitCode.JOB_FAILED,
                                      f"the job exited with code {job.exit_code} (see `gpu-ops logs`)")
                return
            if job.died:
                raise GpuOpsError(ExitCode.JOB_FAILED,
                                  "the job's process disappeared without finishing (killed on the "
                                  "instance, e.g. out of memory); see `gpu-ops logs`")
            if (self.job_deadline() - _utc()).total_seconds() <= 0:
                raise GpuOpsError(ExitCode.CAP_HIT, "the job did not finish before its collection window")
            time.sleep(self.opts.poll_s)

    def _pump_job_log(self) -> None:
        """Copy whatever the job has written since the last poll into the local job.log.

        Done on every poll, not only with `--progress`: the log is the only account of what
        the job did, and a run whose process is killed outright never reaches the full fetch
        at teardown. Keeping the local copy current is what leaves a `kill -9`'d run
        something to read.
        """
        assert self.remote is not None
        lines: list[str] = []
        try:
            self.remote.run(
                f"tail -n +{self._log_echoed + 1} {shlex.quote(self.remote.root)}/job.log 2>/dev/null || true\n",
                timeout_s=120, on_line=lines.append)
        except Exception:
            return  # a poll that could not read the log is not a reason to fail the run
        for line in lines:
            self._job_log(line)  # redacts, writes the local job.log, and echoes if asked
        self._log_echoed += len(lines)

    def _fetch_job_log(self) -> None:
        """Bring the job's log home before the machine goes away. Best effort, always tried.

        The log is the only account of what the job did; losing it to a failed teardown or a
        cap hit would leave the caller with an exit code and nothing to read.
        """
        if self.remote is None:
            return
        lines: list[str] = []
        try:
            self.remote.run(f"cat {shlex.quote(self.remote.root)}/job.log 2>/dev/null || true\n",
                            timeout_s=300, on_line=lines.append)
        except Exception as e:  # a log we could not fetch must not hide the run's own outcome
            self.record.notes.append(f"could not fetch job.log from the instance: {type(e).__name__}")
            return
        # only replace the incrementally pumped copy with one that is at least as complete:
        # a short read from a dying machine must not destroy what we already have
        if lines and len(lines) >= self._log_echoed:
            (self.run_dir / "job.log").write_text(
                "".join(self.redact(line) + "\n" for line in lines), encoding="utf-8")
            self._log_echoed = len(lines)

    def collect_plan(self) -> CollectPlan:
        """What `collect` needs, taken from the spec while the spec is still in hand.

        Built from the live remote's root, not `opts.remote_root`: the fake provider's
        instances live under their own directory, and a plan that named the wrong root
        would look for outputs that were never there.
        """
        assert self.remote is not None
        return CollectPlan.from_spec(self.spec, self.remote.root)

    def _collect(self) -> None:
        assert self.remote is not None
        collect_outputs(self.remote, self.run_dir, self.collect_plan(), self.record)

    def _release(self) -> ExitCode:
        ok = False
        with self._phase("teardown"):
            try:
                if self.instance is None:
                    # create may have succeeded on the provider side even though we never saw the id
                    leftovers = [i for i in self.provider.list_instances(self.name) if i.name == self.name]
                    if leftovers:
                        self.instance = leftovers[0]
                        self.record.instance_id = self.instance.id
                if self.instance is None:
                    ok = True
                else:
                    ok = teardown(self.provider, self.instance.id, self.opts.teardown_timeout_s, self.opts.poll_s)
            except Exception as e:  # never let teardown bookkeeping hide a possibly-live instance
                self.record.notes.append(f"teardown error: {e!r}")
                ok = False
            finally:
                ended = _utc()
                self.record.ended_at = ended.isoformat()
                if self.key_dir:
                    shutil.rmtree(self.key_dir, ignore_errors=True)
                    self.key_dir = None
        self.record.teardown_confirmed = ok
        if self.instance is None:
            self.ledger.event(self.run_id, "terminated" if ok else "unknown")
            return ExitCode.OK if ok else ExitCode.TEARDOWN_FAILED
        finalize(self.record, started=self.started, ended=ended,
                 price_per_hr=self.record.price_per_hr or self.plan.offer.price_per_hr, teardown_ok=ok)
        self.ledger.event(self.run_id, "terminated" if ok else "live", instance_id=self.record.instance_id)
        if not ok:
            self.log(f"TEARDOWN FAILED — INSTANCE MAY STILL BE BILLING: {self.provider.name} {self.record.instance_id}")
            return ExitCode.TEARDOWN_FAILED
        self.log(f"terminated {self.record.instance_id}; est. cost ${self.record.cost_estimate_usd}")
        return ExitCode.OK


def teardown(provider: Provider, instance_id: str, timeout_s: float, poll_s: float = 5) -> bool:
    """Terminate, then confirm with the provider that the instance is gone. Retries with backoff."""
    end = time.monotonic() + timeout_s
    delay = poll_s
    while True:
        try:
            provider.terminate(instance_id)
        except Exception:
            pass
        try:
            inst = provider.get(instance_id)
            if inst is None or inst.status == "terminated":
                return True
        except Exception:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(min(delay, max(0.0, end - time.monotonic())))
        delay = min(delay * 2, 30)
