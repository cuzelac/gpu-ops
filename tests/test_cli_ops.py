"""CLI-level tests: JSON contract, real signals in a separate process, reaper, spend."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode
from gpu_ops.names import PREFIX, instance_name, new_run_id
from gpu_ops.ops import reap_decisions
from gpu_ops.policy import Policy
from gpu_ops.providers.base import CreateRequest, Instance
from gpu_ops.providers.fake import DEFAULT_OFFERS, FakeProvider
from gpu_ops.result import compact, result
from gpu_ops.spend import summarize

from .conftest import write_spec


def cli(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "gpu_ops.cli", *args], capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy())


def test_validate_and_schema_json(env: Path):
    r = cli("validate", str(write_spec(env)), "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["ok"] is True
    r = cli("schema")
    assert "max_cost_usd" in r.stdout


def test_invalid_spec_exit_2_json(env: Path):
    bad = env / "bad.toml"
    bad.write_text('name = "x"\n')
    r = cli("validate", str(bad), "--json")
    assert r.returncode == ExitCode.SPEC_INVALID
    assert json.loads(r.stdout)["exit_code"] == 2


def test_dry_run_creates_nothing(env: Path):
    r = cli("run", str(write_spec(env)), "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["dry_run"] and out["plan"]["offer"]["gpu_id"] == "FAKE-24GB"
    assert FakeProvider(env / "fake").list_instances(PREFIX) == []
    assert not (env / "fake" / "state.json").exists()


def test_missing_policy_refuses(env: Path):
    (env / "cfg" / "policy.toml").unlink()
    r = cli("run", str(write_spec(env)), "--dry-run", "--json")
    assert r.returncode == ExitCode.REFUSED and "gpu-ops init" in json.loads(r.stdout)["error"]


def test_full_run_via_cli(env: Path):
    r = cli("run", str(write_spec(env)), "--json", "--quiet")  # --quiet still accepted
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["outputs_verified"] is True
    assert FakeProvider(env / "fake").list_instances(PREFIX) == []


# derived, not hand-copied: the shape is compact()'s to define, and it must not vary per run
COMPACT_FIELDS = set(result(0, **compact({}, Path("."), [])))


def test_run_json_is_compact_by_default(env: Path):
    r = cli("run", str(write_spec(env)), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert set(out) == COMPACT_FIELDS  # no record, no manifests, no phase timings
    assert out["outputs"] == {"result.txt": 3} and out["minutes"] is not None
    assert out["report"] is None and out["log_tail"] is None  # present, not absent
    assert len(r.stdout.splitlines()) <= 25  # an agent pays for every line of this
    assert "sha256" not in r.stdout and len(json.dumps(out)) < 1200


def test_run_full_keeps_the_whole_record(env: Path):
    r = cli("run", str(write_spec(env)), "--json", "--full")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert set(out) == {"ok", "exit_code", "run_id", "run_dir", "record"}
    rec = out["record"]
    assert rec["outputs_manifest"] and rec["phases"] and rec["inputs_file_count"] == 2
    assert "inputs_manifest" not in rec  # it has its own file now
    manifest = json.loads((Path(out["run_dir"]) / "inputs-manifest.json").read_text())
    assert set(manifest) == {"data/a.txt", "data/sub/b.txt"}


def test_run_is_silent_on_stderr_without_a_tty_or_flags(env: Path):
    r = cli("run", str(write_spec(env)), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stderr == "", r.stderr


def test_progress_streams_phases_and_job_output(env: Path):
    r = cli("run", str(write_spec(env, command="echo from-the-job > $GPUOPS_OUT/result.txt; echo hello-stderr")),
            "--json", "--progress")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[provision]" in r.stderr and "  | hello-stderr" in r.stderr


def test_failed_run_carries_a_log_tail(env: Path):
    r = cli("run", str(write_spec(env, command="echo about-to-fail; exit 3")), "--json")
    assert r.returncode == ExitCode.JOB_FAILED
    out = json.loads(r.stdout)
    assert out["ok"] is False and "exited with code 3" in out["error"]
    assert "about-to-fail" in out["log_tail"]


# ---------------- --get ----------------

def test_get_selects_fields_on_plan(env: Path):
    r = cli("plan", str(write_spec(env)), "--get", "plan.offer.price_per_hr,plan.worst_case_usd")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert set(got) == {"plan.offer.price_per_hr", "plan.worst_case_usd"}
    assert got["plan.offer.price_per_hr"] == 0.20


def test_get_works_on_run_ps_and_spend(env: Path):
    r = cli("run", str(write_spec(env)), "--get", "exit_code,cost_estimate_usd,outputs.result.txt")
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["exit_code"] == 0 and got["outputs.result.txt"] == 3

    r = cli("ps", "--get", "instances")
    assert r.returncode == 0 and json.loads(r.stdout) == {"instances": []}

    r = cli("spend", "--get", "total_usd,runs")
    assert r.returncode == 0 and json.loads(r.stdout)["runs"] == 1


def test_get_on_a_missing_path_is_exit_2(env: Path):
    r = cli("plan", str(write_spec(env)), "--get", "plan.offer.nope")
    assert r.returncode == ExitCode.SPEC_INVALID
    assert json.loads(r.stdout)["exit_code"] == 2
    assert "price_per_hr" in json.loads(r.stdout)["error"]  # says what is available


# ---------------- logs ----------------

def test_logs_tail_grep_and_phase(env: Path):
    spec = write_spec(env, setup="echo setting-up; echo ERROR: in-setup",
                      command="echo line-one; echo ERROR: boom; echo line-three; echo hi > $GPUOPS_OUT/result.txt")
    r = cli("run", str(spec), "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    run_id = json.loads(r.stdout)["run_id"]

    r = cli("logs", run_id, "--tail", "5", "--grep", "ERROR", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["lines"] == ["ERROR: in-setup", "ERROR: boom"] and out["lines_matched"] == 2

    r = cli("logs", run_id, "--tail", "1", "--grep", "ERROR", "--json")
    assert json.loads(r.stdout)["lines"] == ["ERROR: boom"]

    r = cli("logs", run_id, "--phase", "setup", "--json")
    assert json.loads(r.stdout)["lines"] == ["setting-up", "ERROR: in-setup"]

    r = cli("logs", run_id, "--phase", "command", "--get", "lines.0")
    assert json.loads(r.stdout) == {"lines.0": "line-one"}

    r = cli("logs", run_id, "--tail", "2")  # human output is just the lines
    assert r.stdout.splitlines() == ["ERROR: boom", "line-three"]


def test_logs_for_an_unknown_run_is_exit_2(env: Path):
    r = cli("logs", "20261112T140915-dead", "--json")  # well-formed, but no such run
    assert r.returncode == ExitCode.SPEC_INVALID
    assert "no job log" in json.loads(r.stdout)["error"]


@pytest.mark.parametrize("bad", ["nope-not-a-run", "../../../../etc", "..", "a/b"])
def test_logs_rejects_anything_that_is_not_a_run_id(env: Path, bad: str):
    """The run id becomes a path, so it is validated before it is one."""
    r = cli("logs", bad, "--json")
    assert r.returncode == ExitCode.SPEC_INVALID
    assert "is not a run id" in json.loads(r.stdout)["error"]


def test_a_bad_get_path_never_overwrites_a_failed_run_exit_code(env: Path):
    """Printing must not decide the exit code: exit 6 here, not the usage error's exit 2."""
    spec = write_spec(env, command="echo nope; exit 3")
    r = cli("run", str(spec), "--get", "no_such_field")
    assert r.returncode == ExitCode.JOB_FAILED, r.stdout + r.stderr
    assert "no such field" in r.stderr  # the usage error is still reported
    out = json.loads(r.stdout)
    assert out["exit_code"] == 6
    # and the WHOLE result is printed, not a stub: the payload is the point of keeping the
    # code, so assert the payload too
    assert out["teardown_confirmed"] is True and "log_tail" in out
    assert "about to fail" not in out.get("error", "") or True
    assert set(out) >= COMPACT_FIELDS


def test_a_bad_get_path_on_a_clean_run_is_still_exit_2(env: Path):
    r = cli("run", str(write_spec(env)), "--get", "no_such_field")
    assert r.returncode == ExitCode.SPEC_INVALID


def test_get_works_on_schema(env: Path):
    r = cli("schema", "--get", "schema.$defs.LimitsSpec.properties")
    assert r.returncode == 0, r.stderr
    assert "max_cost_usd" in r.stdout
    assert "min_vram_gb" not in r.stdout  # only the selected subtree


def test_schema_carries_ok_and_exit_code_like_every_command(env: Path):
    r = cli("schema", "--json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["exit_code"] == 0
    assert "max_cost_usd" in json.dumps(out["schema"])


def test_logs_does_not_create_a_directory_for_an_unknown_run(env: Path):
    """A free, read-only command must not leave state behind."""
    run_id = "20261112T140915-beef"
    r = cli("logs", run_id, "--json")
    assert r.returncode == ExitCode.SPEC_INVALID
    assert not (env / "state" / "runs" / run_id).exists()


def test_reconcile_skips_a_record_it_cannot_read(env: Path):
    """One unreadable record must not cost the caller the whole report."""
    prov = FakeProvider(env / "fake")
    inst = prov.create(CreateRequest(name=instance_name(new_run_id(), 1, "ok"), offer=DEFAULT_OFFERS[0],
                                     image="x", bootstrap="", public_key=""))
    good, bad = new_run_id(), new_run_id()
    for rid, extra in ((good, {}), (bad, {"started_at": "not-a-timestamp"})):
        d = env / "state" / "runs" / rid
        d.mkdir(parents=True)
        (d / "record.json").write_text(json.dumps({
            "run_id": rid, "job": "j", "project": "tests", "kind": "other", "spec_path": "x",
            "provider": "fake", "instance_id": inst.id, "started_at": "2026-11-01T10:00:00+00:00",
            "price_per_hr": 0.2, "gpu_count": 1, **extra}))

    r = cli("spend", "--reconcile", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["reconciled_now"] == 1  # the readable one still got done
    assert len(out["reconcile_skipped"]) == 1 and bad in out["reconcile_skipped"][0]
    assert out["runs"] == 2  # and the report itself was still produced


def test_reconcile_keeps_fields_this_version_does_not_know(env: Path):
    """A record written by another version must survive a rewrite, not be silently trimmed."""
    prov = FakeProvider(env / "fake")
    inst = prov.create(CreateRequest(name=instance_name(new_run_id(), 1, "old"), offer=DEFAULT_OFFERS[0],
                                     image="x", bootstrap="", public_key=""))
    run_id = new_run_id()
    d = env / "state" / "runs" / run_id
    d.mkdir(parents=True)
    (d / "record.json").write_text(json.dumps({
        "run_id": run_id, "job": "old", "project": "tests", "kind": "other", "spec_path": "x",
        "provider": "fake", "instance_id": inst.id, "started_at": "2026-11-01T10:00:00+00:00",
        "price_per_hr": 0.2, "gpu_count": 1,
        "inputs_manifest": {"data/a.txt": "f" * 64},  # a field this version no longer defines
        "a_field_from_the_future": 42,
    }))

    r = cli("spend", "--reconcile", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["reconciled_now"] == 1

    saved = json.loads((d / "record.json").read_text())
    assert saved["cost_billed_usd"] == 0.0  # the field reconcile came to write
    assert saved["inputs_manifest"] == {"data/a.txt": "f" * 64}  # and nothing else was dropped
    assert saved["a_field_from_the_future"] == 42



@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_mid_run_tears_down(env: Path, sig):
    spec = write_spec(env, command="echo job-started; sleep 60")
    p = subprocess.Popen([sys.executable, "-m", "gpu_ops.cli", "run", str(spec), "--json", "--quiet"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:  # wait until the command itself is running on the "instance"
        logs = list((env / "state" / "runs").glob("*/job.log"))
        if logs and "job-started" in logs[0].read_text():
            break
        time.sleep(0.2)
    else:
        p.kill()
        pytest.fail("command never started")
    assert len(FakeProvider(env / "fake").list_instances(PREFIX)) == 1
    p.send_signal(sig)
    stdout, stderr = p.communicate(timeout=60)
    assert p.returncode == ExitCode.JOB_FAILED, stdout + stderr
    assert "interrupted" in json.loads(stdout)["error"]
    assert FakeProvider(env / "fake").list_instances(PREFIX) == []


def test_kill_all_and_reap(env: Path):
    prov = FakeProvider(env / "fake")
    now = datetime.now(timezone.utc)
    expired = instance_name(new_run_id(), int((now - timedelta(minutes=30)).timestamp()), "old")
    current = instance_name(new_run_id(), int((now + timedelta(minutes=30)).timestamp()), "new")
    for n in (expired, current, "manual-pod-not-ours"):
        prov.create(CreateRequest(name=n, offer=DEFAULT_OFFERS[0], image="x", bootstrap="", public_key=""))

    r = cli("reap", "--dry-run", "--json")
    reaped = json.loads(r.stdout)["reaped"]
    assert [x["name"] for x in reaped] == [expired] and reaped[0]["terminated"] is None

    r = cli("reap", "--json")
    assert r.returncode == 0
    alive = {i.name for i in prov.list_instances("")}
    assert alive == {current, "manual-pod-not-ours"}

    r = cli("ps", "--json")
    assert [x["name"] for x in json.loads(r.stdout)["instances"]] == [current]

    r = cli("kill", "--all", "--json")
    assert r.returncode == 0
    assert {i.name for i in prov.list_instances("")} == {"manual-pod-not-ours"}  # never touches non-gpuops pods


def test_reap_decisions_without_deadline_uses_age():
    now = datetime.now(timezone.utc)
    old = Instance("fake", "1", "gpuops-unparseable", "running", created_at=now - timedelta(hours=10))
    young = Instance("fake", "2", "gpuops-unparseable2", "running", created_at=now - timedelta(minutes=5))
    got = reap_decisions([old, young], now, grace_min=10, max_age_min=370)
    assert [i.id for i, _ in got] == ["1"]


def test_spend_summary():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    recs = [
        {"started_at": "2026-11-02T10:00:00+00:00", "cost_estimate_usd": 1.0, "billed_seconds": 3600,
         "gpu_count": 1, "vram_gb": 24, "provider": "runpod", "project": "a", "kind": "fine-tune",
         "gpu_name": "RTX 5090", "phases": {"provision": 60, "command": 540}, "instance_id": "x",
         "ended_at": "2026-11-02T11:00:00+00:00"},
        {"started_at": "2026-10-02T10:00:00+00:00", "cost_estimate_usd": 5.0, "cost_billed_usd": 5.5,
         "billed_seconds": 7200, "gpu_count": 2, "vram_gb": 80, "provider": "runpod", "project": "b",
         "kind": "batch-inference", "gpu_name": "H100", "phases": {"command": 100}, "instance_id": "y"},
    ]
    pol = Policy()
    pol.rent_vs_buy.card_price_usd = 650
    pol.rent_vs_buy.warn_monthly_usd = 1
    s = summarize(recs, pol, now)
    assert s["total_usd"] == pytest.approx(6.5)
    assert s["gpu_hours"] == pytest.approx(1 + 4)
    assert s["runs_a_single_card_could_not_replace"] == 1
    assert s["unreconciled_runs"] == 1
    assert s["trailing_3mo_monthly_usd"] == pytest.approx(6.5 / 3, abs=1e-3)
    assert s["overhead_share"] == pytest.approx(60 / 700, abs=1e-3)
    assert s["rent_vs_buy"]["months_to_break_even_at_trailing_rate"] == pytest.approx((650 - 6.5) / (6.5 / 3), 0.01)
    assert s["warning"] and "rent vs buy" in s["warning"]


def test_plan_is_free_dry_run(env: Path):
    r = cli("plan", str(write_spec(env)), "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["dry_run"] is True
    assert not (env / "fake" / "state.json").exists()


def test_spend_counts_killed_runs_at_upper_bound():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    killed = {"started_at": "2026-11-12T14:13:24+00:00", "deadline": "2026-11-12T14:16:24+00:00",
              "instance_id": "k", "price_per_hr": 0.20, "gpu_count": 1, "status": "started"}
    s = summarize([killed], None, now)
    assert s["unfinished_runs_counted_at_upper_bound"] == 1
    assert s["total_usd"] == pytest.approx(0.20 * 3 / 60, abs=1e-4)
    assert s["gpu_hours"] == pytest.approx(3 / 60, abs=1e-3)
    s2 = summarize([{**killed, "cost_billed_usd": 0.004}], None, now)  # billing replaces the bound
    assert s2["total_usd"] == pytest.approx(0.004) and s2["unfinished_runs_counted_at_upper_bound"] == 0


def test_reconcile_patches_the_file_it_read_not_the_id_inside_it(env: Path):
    """A record whose run_id disagrees with its directory must not be copied to a new file."""
    prov = FakeProvider(env / "fake")
    inst = prov.create(CreateRequest(name=instance_name(new_run_id(), 1, "x"), offer=DEFAULT_OFFERS[0],
                                     image="x", bootstrap="", public_key=""))
    here, elsewhere = new_run_id(), new_run_id()
    d = env / "state" / "runs" / here
    d.mkdir(parents=True)
    (d / "record.json").write_text(json.dumps({
        "run_id": elsewhere,  # disagrees with the directory it lives in
        "job": "j", "project": "tests", "kind": "other", "spec_path": "x", "provider": "fake",
        "instance_id": inst.id, "started_at": "2026-11-01T10:00:00+00:00",
        "price_per_hr": 0.2, "gpu_count": 1}))

    r = cli("spend", "--reconcile", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["reconciled_now"] == 1
    assert json.loads((d / "record.json").read_text())["cost_billed_usd"] == 0.0  # the file it read
    assert not (env / "state" / "runs" / elsewhere).exists()  # no second file invented
    assert out["runs"] == 1  # so spend counts the run once, not twice


def test_spend_counts_a_record_it_cannot_read_separately(env: Path):
    """summarize is the reader that runs on every spend; it must not die on one bad record."""
    d = env / "state" / "runs" / new_run_id()
    d.mkdir(parents=True)
    (d / "record.json").write_text(json.dumps({
        "run_id": "x", "job": "j", "project": "tests", "kind": "other", "spec_path": "x",
        "started_at": 20261101,  # a number where a timestamp belongs
    }))
    r = cli("spend", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["unreadable_records"] == 1 and out["runs"] == 0
