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
    r = cli("run", str(write_spec(env)), "--json", "--quiet")
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["record"]["outputs_verified"] is True
    assert FakeProvider(env / "fake").list_instances(PREFIX) == []


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
    assert "interrupted" in json.loads(stdout)["record"]["error"]
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
