"""Detached runs end to end: nothing is left billing, and outputs are never quietly lost.

These drive the real CLI in a subprocess, because the point of a detached run is that it
survives the process that started it — the launching process is genuinely gone here, not
mocked out.

Every test asserts the result payload, not only the exit code: a stub carries the right
code too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode
from gpu_ops.names import PREFIX
from gpu_ops.providers.fake import FakeProvider

from .conftest import write_spec


def cli(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "gpu_ops.cli", *args], capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy())


def detach(env: Path, **spec_kw) -> dict:
    spec = write_spec(env, **spec_kw)
    t0 = time.monotonic()
    r = cli("run", str(spec), "--detach", "--json")
    took = time.monotonic() - t0
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    # the acceptance criterion: a detached launch returns at once, however long the job is
    assert took < 10, f"--detach took {took:.1f}s to return"
    assert out["detached"] is True and out["status"] == "running"
    assert out["instance_id"] and out["collect_by"]
    # the run is deliberately not finished: saying otherwise is the dangerous lie here
    assert out["teardown_confirmed"] is None and out["outputs_verified"] is None
    return out


def alive(env: Path) -> list:
    return FakeProvider(env / "fake").list_instances(PREFIX)


def test_detach_then_wait_brings_outputs_home_and_terminates(env: Path):
    out = detach(env, command="cat $GPUOPS_IN/data/a.txt > $GPUOPS_OUT/result.txt")
    assert len(alive(env)) == 1, "a detached run must leave its machine running"

    r = cli("wait", out["run_id"], "--json", "--poll", "0.2")
    assert r.returncode == ExitCode.OK, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["outputs_verified"] is True and res["teardown_confirmed"] is True
    assert res["cost_estimate_usd"] is not None
    assert res["outputs"]["result.txt"] > 0
    out_file = Path(res["outputs_dir"]) / "result.txt"
    assert out_file.read_text() == "alpha\n"
    assert alive(env) == [], "wait must leave nothing billing"

    # `wait` prints exactly one JSON object and streams nothing
    assert len(json.JSONDecoder().raw_decode(r.stdout.strip())[0]) > 0
    assert r.stdout.strip().count("\n{") == 0


def test_collect_succeeds_after_the_launching_process_is_gone(env: Path):
    """The whole point: a different process, with no spec in hand, finishes the run."""
    out = detach(env, command="cat $GPUOPS_IN/data/sub/b.txt > $GPUOPS_OUT/result.txt")
    run_id = out["run_id"]
    # the spec is deleted outright: `collect` must not need it
    (env / "job.toml").unlink()

    for _ in range(100):
        s = json.loads(cli("status", run_id, "--json").stdout)
        if s["phase"] in ("finished", "died"):
            break
        time.sleep(0.2)
    else:
        pytest.fail("job never finished")

    r = cli("collect", run_id, "--json")
    assert r.returncode == ExitCode.OK, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["outputs_verified"] is True and res["teardown_confirmed"] is True
    assert (Path(res["outputs_dir"]) / "result.txt").read_text() == "beta\n"
    assert alive(env) == []


def test_collect_is_idempotent(env: Path):
    out = detach(env, command="cat $GPUOPS_IN/data/a.txt > $GPUOPS_OUT/result.txt")
    r = cli("wait", out["run_id"], "--json", "--poll", "0.2")
    assert r.returncode == ExitCode.OK, r.stdout + r.stderr

    again = cli("collect", out["run_id"], "--json")
    assert again.returncode == ExitCode.OK, again.stdout + again.stderr
    res = json.loads(again.stdout)
    assert res["teardown_confirmed"] is True and res["outputs_verified"] is True
    assert alive(env) == [], "a second collect must not create or revive anything"


def test_collect_while_running_changes_nothing_and_is_exit_11(env: Path):
    out = detach(env, command="sleep 30; echo done > $GPUOPS_OUT/result.txt")
    r = cli("collect", out["run_id"], "--json")
    assert r.returncode == ExitCode.STILL_RUNNING, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["exit_code"] == 11 and res["ok"] is False
    assert res["teardown_confirmed"] is None, "nothing was torn down"
    assert len(alive(env)) == 1, "the job was still working: its machine must be untouched"

    assert cli("kill", "--all", "--json").returncode == 0  # don't leave the fake pod around


def test_remote_failure_is_exit_6_and_still_terminates(env: Path):
    out = detach(env, command="echo boom; exit 3")
    r = cli("wait", out["run_id"], "--json", "--poll", "0.2")
    assert r.returncode == ExitCode.JOB_FAILED, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert "exited with code 3" in res["error"]
    assert res["teardown_confirmed"] is True, "a failed job must still have its machine deleted"
    assert any("boom" in line for line in res["log_tail"]), "the job's log must come home too"
    assert alive(env) == []


def test_deadline_before_collect_loses_outputs_and_leaves_nothing_alive(env: Path):
    """The dead-man timer wins the race, and the record says so plainly."""
    out = detach(env, command="cat $GPUOPS_IN/data/a.txt > $GPUOPS_OUT/result.txt")
    run_id = out["run_id"]
    # the dead-man timer's own job, done by hand so the test does not wait for a deadline
    for inst in alive(env):
        FakeProvider(env / "fake").terminate(inst.id)
    assert alive(env) == []

    r = cli("collect", run_id, "--json")
    assert r.returncode == ExitCode.TRANSFER_FAILED, r.stdout + r.stderr
    res = json.loads(r.stdout)
    assert res["exit_code"] == 7
    assert "lost" in res["error"]
    assert res["outputs_verified"] is False, "outputs that never arrived are not verified"
    assert res["teardown_confirmed"] is True, "the machine is confirmed gone, which is what matters"
    assert alive(env) == []

    saved = json.loads((Path(res["outputs_dir"]).parent / "record.json").read_text())
    assert saved["status"] == "failed" and saved["exit_code"] == 7


def test_status_reports_a_job_that_died_without_finishing(env: Path):
    out = detach(env, command="sleep 300")
    run_id = out["run_id"]
    for _ in range(100):
        s = json.loads(cli("status", run_id, "--json").stdout)
        if s["phase"] == "command":
            break
        time.sleep(0.1)
    else:
        pytest.fail("job never reached the command phase")

    # kill the resident job the way the OOM killer would, leaving status.json mid-phase
    root = Path(json.loads((Path(env) / "fake" / "state.json").read_text())["instances"]
                [out["instance_id"]]["id"])
    status = json.loads((env / "fake" / out["instance_id"] / "status.json").read_text())
    os.kill(status["pid"], 9)

    s = json.loads(cli("status", run_id, "--json").stdout)
    assert s["phase"] == "died", "a job whose process is gone must not read as still running"

    r = cli("collect", run_id, "--json")
    assert r.returncode == ExitCode.JOB_FAILED, r.stdout + r.stderr
    assert "disappeared" in json.loads(r.stdout)["error"]
    assert alive(env) == [], "a died job's machine must still be deleted"


def test_status_is_free_and_changes_nothing(env: Path):
    out = detach(env, command="sleep 20")
    before = len(alive(env))
    s = json.loads(cli("status", out["run_id"], "--json").stdout)
    assert s["ok"] is True and s["exit_code"] == 0, "status is an observation, like ps"
    assert s["run_id"] == out["run_id"] and s["collected"] is False
    assert s["minutes_to_deadline"] is not None and s["cost_so_far_usd"] is not None
    assert len(alive(env)) == before
    assert cli("kill", "--all", "--json").returncode == 0


def test_status_on_an_unknown_run_is_exit_2(env: Path):
    r = cli("status", "20261112T140915-dead", "--json")
    assert r.returncode == ExitCode.SPEC_INVALID
    assert "no run" in json.loads(r.stdout)["error"]


def test_detached_run_in_flight_is_not_reported_as_died(env: Path):
    """`spend` costs an in-flight run at its upper bound, but must not call it dead."""
    out = detach(env, command="sleep 30")
    r = cli("spend", "--json")
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["runs_in_flight"] == 1
    assert s["unfinished_runs_counted_at_upper_bound"] == 0, (
        "a healthy detached run must not be counted as one that died before teardown")
    assert s["total_usd"] > 0, "its worst case is still reserved against the monthly cap"
    assert cli("kill", "--all", "--json").returncode == 0
    assert out["run_id"]
