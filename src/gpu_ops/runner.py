"""The run lifecycle: acquire → execute → release, with teardown guaranteed.

Every path out of `Runner.run()` — success, job failure, exception, Ctrl-C, SIGTERM —
goes through `_release()`, which deletes the instance and confirms it is gone.
Kept as three parts so sessions (acquire once, execute many, release once) can be added
later without a rewrite.
"""

from __future__ import annotations

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
from .secrets import Redactor
from .spec import LoadedSpec

RemoteFactory = Callable[[Instance, Path], "Remote | None"]
Log = Callable[[str], None]

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
                 opts: RunOptions | None = None, log: Log | None = None):
        self.loaded, self.spec, self.plan, self.policy = loaded, loaded.spec, plan, policy
        self.provider, self.ledger, self.remote_factory = provider, ledger, remote_factory
        self.redact, self.secrets = redactor, secrets
        self.opts = opts or RunOptions()
        self.log = log or (lambda s: None)

        self.run_id = new_run_id()
        self.started = _utc()
        self.instance: Instance | None = None
        self.remote: Remote | None = None
        self.key_dir: Path | None = None
        self.run_dir = ledger.run_dir(self.run_id)
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

    def _job_log(self, line: str) -> None:
        line = self.redact(line)
        with (self.run_dir / "job.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.log("  | " + line)

    def _prelude(self) -> str:
        r = self.opts.remote_root if not self.remote else self.remote.root
        lines = ["set -euo pipefail",
                 f"export GPUOPS_ROOT={shlex.quote(r)} GPUOPS_IN={shlex.quote(r + '/in')} "
                 f"GPUOPS_OUT={shlex.quote(r + '/out')} GPUOPS_WORK={shlex.quote(r + '/work')} "
                 f"GPUOPS_RUN_ID={shlex.quote(self.run_id)}"]
        lines += [f"export {k}={shlex.quote(v)}" for k, v in self.secrets.items()]
        lines += [f"export {k}={_dq(v)}" for k, v in self.spec.env.items()]
        lines.append('mkdir -p "$GPUOPS_WORK" "$GPUOPS_OUT" && cd "$GPUOPS_WORK"')
        return "\n".join(lines) + "\n"

    def _make_key(self) -> str:
        import os
        base = os.environ.get("XDG_RUNTIME_DIR") or ("/dev/shm" if Path("/dev/shm").is_dir() else None)
        self.key_dir = Path(tempfile.mkdtemp(prefix="gpuops-key-", dir=base))
        key = self.key_dir / "id_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"gpuops-{self.run_id}", "-f", str(key)],
                       check=True, capture_output=True)
        return (self.key_dir / "id_ed25519.pub").read_text().strip()

    # ---------- lifecycle ----------

    def run(self) -> tuple[ExitCode, RunRecord]:
        code = ExitCode.OK
        self.record.started_at = self.started.isoformat()
        self.ledger.event(self.run_id, "creating", provider=self.provider.name, name=self.name,
                          deadline=self.deadline.isoformat(), job=self.spec.name, project=self.spec.project)
        try:
            self._hash_inputs()
            self._acquire()
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
            code = worst(code, self._release())
        self.record.exit_code = int(code)
        self.record.status = "ok" if code == ExitCode.OK else "failed"
        if self.record.error:
            self.record.error = self.redact(self.record.error)
        self.ledger.write_record(self.record)
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
        self.record.inputs_manifest = full
        self.record.inputs_hash = manifest_digest(full)

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
        assert self.remote is not None
        remote = self.remote
        with self._phase("upload"):
            for inp in self.spec.inputs:
                remote.put(self.loaded.input_paths[inp.name], f"in/{inp.name}", inp.include or None)
            if self.record.inputs_manifest:
                out: list[str] = []
                rels = sorted({k.split("/", 1)[0] for k in self.record.inputs_manifest})
                remote.run(remote_manifest_script(remote.root + "/in", rels), timeout_s=600, on_line=out.append)
                got, _ = parse_sha256sum("\n".join(out))
                problems = compare(self.record.inputs_manifest, got, missing="did not arrive on the machine")
                if problems:
                    raise GpuOpsError(ExitCode.TRANSFER_FAILED, "upload verification failed: " + "; ".join(problems[:5]))

        for phase, lines in (("setup", self.spec.run.setup), ("command", [self.spec.run.command])):
            if not lines:
                continue
            with self._phase(phase):
                budget = self._remaining(reserve=True)
                if budget <= 0:
                    raise GpuOpsError(ExitCode.CAP_HIT, f"no time left for {phase}")
                rc = remote.run(self._prelude() + "\n".join(lines) + "\n", timeout_s=budget, on_line=self._job_log)
                if rc == TIMEOUT_EXIT:
                    raise GpuOpsError(ExitCode.CAP_HIT, f"{phase} hit the deadline and was killed")
                if rc != 0:
                    raise GpuOpsError(ExitCode.JOB_FAILED, f"{phase} exited with code {rc} (see job.log)")

        with self._phase("download"):
            self._collect()

    def _collect(self) -> None:
        assert self.remote is not None
        rels = [o.path for o in self.spec.outputs]
        if not rels:
            self.record.outputs_verified = True
            return
        out_lines: list[str] = []
        rc = self.remote.run(remote_manifest_script(self.remote.root + "/out", rels), timeout_s=900,
                             on_line=out_lines.append)
        if rc != 0:
            raise GpuOpsError(ExitCode.TRANSFER_FAILED, f"could not list outputs on the instance (exit {rc})")
        expected, missing = parse_sha256sum("\n".join(out_lines))
        local_out = self.run_dir / "out"
        for o in self.spec.outputs:
            if o.path not in missing:
                self.remote.get(f"out/{o.path}", self.run_dir)
        got = build_manifest(local_out, [r for r in rels if r not in missing and (local_out / r).exists()])
        self.record.outputs_manifest = got
        problems = compare(expected, got)
        self.record.outputs_verified = not problems
        required_missing = [o.path for o in self.spec.outputs if o.required and o.path in missing]
        if problems:
            raise GpuOpsError(ExitCode.TRANSFER_FAILED, "output verification failed: " + "; ".join(problems[:5]))
        if required_missing:
            raise GpuOpsError(ExitCode.JOB_FAILED, f"required outputs missing: {required_missing}")

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
        self.record.teardown_confirmed = ok
        if self.instance is None:
            self.ledger.event(self.run_id, "terminated" if ok else "unknown")
            return ExitCode.OK if ok else ExitCode.TEARDOWN_FAILED
        if self.instance is not None:
            secs = (ended - self.started).total_seconds()
            self.record.billed_seconds = round(secs, 1)
            price = self.record.price_per_hr or self.plan.offer.price_per_hr
            self.record.cost_estimate_usd = round(price * secs / 3600, 4)
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
