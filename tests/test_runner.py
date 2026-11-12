"""Lifecycle tests: every way a run can end must leave zero live instances (or say exit 10)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode
from gpu_ops.ledger import Ledger
from gpu_ops.names import PREFIX
from gpu_ops.planner import make_plan
from gpu_ops.policy import load_policy
from gpu_ops.providers.fake import FakeProvider
from gpu_ops.remote import LocalRemote
from gpu_ops.runner import Runner, RunOptions
from gpu_ops.secrets import Redactor
from gpu_ops.spec import load_spec

from .conftest import write_spec

FAST = RunOptions(teardown_timeout_s=1, poll_s=0.05)


def run_job(env: Path, *, remote_cls=LocalRemote, secrets=None, policy_edit=None, deadman=False, **spec_kw):
    provider_kw = spec_kw.pop("provider_kw", {})
    loaded = load_spec(write_spec(env, **spec_kw))
    policy = load_policy()
    if policy_edit:
        policy_edit(policy)
    prov = FakeProvider(deadman_armable=deadman, **provider_kw)
    plan = make_plan(loaded.spec, policy, {"fake": prov}, [])
    red = Redactor()
    for v in (secrets or {}).values():
        red.add(v)
    runner = Runner(loaded, plan, policy, prov, Ledger(), lambda i, k: remote_cls(prov.root / i.id), red,
                    secrets or {}, opts=FAST)
    code, rec = runner.run()
    return code, rec, prov, runner


def assert_nothing_alive(prov: FakeProvider):
    assert prov.list_instances(PREFIX) == []


def test_success_brings_back_verified_outputs(env: Path):
    code, rec, prov, runner = run_job(env, command="cat $GPUOPS_IN/data/a.txt > $GPUOPS_OUT/result.txt")
    assert code == ExitCode.OK, rec.error
    assert rec.outputs_verified and rec.teardown_confirmed
    out = runner.run_dir / "out" / "result.txt"
    assert out.read_text() == "alpha\n"
    assert set(rec.inputs_manifest) == {"data/a.txt", "data/sub/b.txt"}
    assert {"provision", "upload", "command", "download", "teardown"} <= set(rec.phases)
    assert rec.cost_estimate_usd is not None and rec.deadman == "unarmed"
    saved = json.loads((runner.run_dir / "record.json").read_text())
    assert saved["exit_code"] == 0 and saved["status"] == "ok"
    assert_nothing_alive(prov)


def test_env_expansion_and_setup(env: Path):
    code, rec, prov, _ = run_job(
        env, setup="echo setup-ran > $GPUOPS_WORK/marker",
        command="test -f $GPUOPS_WORK/marker && cat $DATA_DIR/sub/b.txt > $GPUOPS_OUT/result.txt",
        extra='[env]\nDATA_DIR = "$GPUOPS_IN/data"\n')
    assert code == ExitCode.OK, rec.error
    assert "setup" in rec.phases
    assert_nothing_alive(prov)


def test_job_failure_still_tears_down(env: Path):
    code, rec, prov, _ = run_job(env, command="echo boom; exit 3")
    assert code == ExitCode.JOB_FAILED and "exited with code 3" in rec.error
    assert rec.teardown_confirmed
    assert_nothing_alive(prov)


def test_missing_required_output(env: Path):
    code, rec, prov, _ = run_job(env, command="true")
    assert code == ExitCode.JOB_FAILED and "required outputs missing" in rec.error
    assert_nothing_alive(prov)


def test_optional_output_may_be_missing(env: Path):
    code, rec, prov, _ = run_job(env, command="true", outputs='[[outputs]]\npath = "maybe.txt"\nrequired = false\n')
    assert code == ExitCode.OK, rec.error
    assert_nothing_alive(prov)


class CorruptingRemote(LocalRemote):
    def get(self, remote_rel, local):
        super().get(remote_rel, local)
        for f in (local / remote_rel).parent.rglob("*"):
            if f.is_file():
                f.write_text("corrupted")


def test_checksum_mismatch_fails_and_tears_down(env: Path):
    code, rec, prov, _ = run_job(env, remote_cls=CorruptingRemote)
    assert code == ExitCode.TRANSFER_FAILED and "checksum mismatch" in rec.error
    assert rec.outputs_verified is False
    assert_nothing_alive(prov)


def test_time_cap_kills_job(env: Path):
    t0 = time.monotonic()
    code, rec, prov, _ = run_job(env, command="sleep 60", limits="max_minutes = 0.1\nmax_cost_usd = 1.0")
    assert code == ExitCode.CAP_HIT, rec.error
    assert time.monotonic() - t0 < 30
    assert_nothing_alive(prov)


def test_teardown_retries_transient_failures(env: Path):
    code, rec, prov, _ = run_job(env, provider_kw={"terminate_failures": 2})
    assert code == ExitCode.OK and rec.teardown_confirmed
    assert_nothing_alive(prov)


def test_teardown_failure_is_exit_10_and_wins(env: Path):
    code, rec, prov, _ = run_job(env, command="exit 1", provider_kw={"terminate_failures": 10_000})
    assert code == ExitCode.TEARDOWN_FAILED  # even though the job also failed
    assert rec.teardown_confirmed is False
    assert len(prov.list_instances(PREFIX)) == 1
    state = Ledger().current()[rec.run_id]
    assert state["status"] == "live"


def test_create_failure(env: Path):
    code, rec, prov, _ = run_job(env, provider_kw={"fail_create": True})
    assert code == ExitCode.PROVIDER_ERROR and rec.instance_id is None
    assert_nothing_alive(prov)


class InterruptingRemote(LocalRemote):
    def run(self, script, *, timeout_s=None, on_line=None):
        if "GPUOPS_RUN_ID" in script:  # the setup/command script, not manifest checks
            raise KeyboardInterrupt
        return super().run(script, timeout_s=timeout_s, on_line=on_line)


def test_interrupt_tears_down(env: Path):
    code, rec, prov, _ = run_job(env, remote_cls=InterruptingRemote)
    assert code == ExitCode.JOB_FAILED and "interrupted" in rec.error
    assert_nothing_alive(prov)


def test_unarmed_deadman_refuses_long_runs(env: Path):
    def tighten(p):
        p.limits.allow_unarmed_deadman_minutes = 1
    code, rec, prov, _ = run_job(env, policy_edit=tighten, limits="max_minutes = 5\nmax_cost_usd = 1.0")
    assert code == ExitCode.REFUSED and "dead-man" in rec.error
    assert_nothing_alive(prov)


def test_deadman_arms_and_fires(env: Path):
    code, rec, prov, _ = run_job(env, deadman=True, limits="max_minutes = 0.05\nmax_cost_usd = 1.0")
    assert rec.deadman == "armed"
    marker = prov.root / "deadman-fired"
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists(), "dead-man timer did not fire"


def test_secrets_reach_the_job_but_not_the_log(env: Path):
    secret = "s3cr3t-value-123"
    code, rec, prov, runner = run_job(env, secrets={"TOKEN": secret},
                                      command='echo "token=$TOKEN"; echo -n "$TOKEN" | wc -c > $GPUOPS_OUT/result.txt')
    assert code == ExitCode.OK, rec.error
    log = (runner.run_dir / "job.log").read_text()
    assert secret not in log and "token=***" in log
    assert (runner.run_dir / "out" / "result.txt").read_text().strip() == str(len(secret))
    assert secret not in (runner.run_dir / "record.json").read_text()


def test_include_limits_upload(env: Path):
    spec_extra_inputs = '[[outputs]]\npath = "result.txt"\n'
    code, rec, prov, _ = run_job(env, outputs=spec_extra_inputs, inputs=False,
                                 extra='[[inputs]]\nname = "d"\nfrom = "data"\ninclude = ["sub/b.txt"]\n',
                                 command="ls -R $GPUOPS_IN > $GPUOPS_OUT/result.txt")
    assert code == ExitCode.OK, rec.error
    assert set(rec.inputs_manifest) == {"d/sub/b.txt"}


def test_capacity_fallback_to_next_offer(env: Path):
    code, rec, prov, _ = run_job(env, provider_kw={"no_capacity": {("FAKE-24GB", "community")}})
    assert code == ExitCode.OK, rec.error
    assert rec.tier == "secure" and rec.price_per_hr == 0.30  # next cheapest
    assert any("fell back" in n for n in rec.notes)
    assert_nothing_alive(prov)


def test_capacity_fallback_respects_cost_cap(env: Path):
    # only the $1.20 offer would remain; at max_cost_usd 0.10 it gets a 5-minute deadline, not a refusal
    code, rec, prov, _ = run_job(env, limits="max_minutes = 30\nmax_cost_usd = 0.10",
                                 provider_kw={"no_capacity": {("FAKE-24GB", "community"), ("FAKE-24GB", "secure")}})
    assert code == ExitCode.OK, rec.error
    assert rec.price_per_hr == 1.20 and rec.worst_case_usd <= 0.10 + 1e-9
    assert_nothing_alive(prov)


def test_capacity_everywhere_is_exit_4(env: Path):
    code, rec, prov, _ = run_job(env, provider_kw={"no_capacity": {("FAKE-24GB", "community"), ("FAKE-24GB", "secure"),
                                                                    ("FAKE-80GB", "community")}})
    assert code == ExitCode.NO_OFFER and rec.instance_id is None
    assert_nothing_alive(prov)


def test_unarmed_deadman_records_diagnosis(env: Path):
    code, rec, prov, _ = run_job(env)
    assert rec.deadman == "unarmed"
    assert any(n.startswith("dead-man diagnosis:") and "FAKE_KEY=missing" in n for n in rec.notes)


class RecordCheckingRemote(LocalRemote):
    def run(self, script, *, timeout_s=None, on_line=None):
        if "GPUOPS_RUN_ID" in script:
            recs = list((Path(self.root).parents[1] / "state" / "runs").glob("*/record.json"))
            assert recs and '"status": "started"' in recs[0].read_text(), "record must exist while the job runs"
        return super().run(script, timeout_s=timeout_s, on_line=on_line)


def test_record_written_before_job_runs(env: Path):
    code, rec, prov, _ = run_job(env, remote_cls=RecordCheckingRemote)
    assert code == ExitCode.OK, rec.error
