"""The resident job: the launcher really detaches, and status.json really says what happened.

These run the launcher for real against `LocalRemote`, because the whole point of the
design is what survives the process that started it — a mocked remote would prove nothing.
"""

from __future__ import annotations

import time
from pathlib import Path

from gpu_ops.remote import LocalRemote
from gpu_ops.resident import (env_file_script, launcher_script, parse_status, phase_script,
                              status_script)


def _launch(remote: LocalRemote, phases: dict[str, str], *, deadline_in: int = 60,
            exports: dict[str, str] | None = None) -> None:
    root = remote.root
    if exports:
        assert remote.run(env_file_script(root, exports)) == 0
    for name, body in phases.items():
        assert remote.run(phase_script(root, name, body)) == 0
    t0 = time.monotonic()
    rc = remote.run(launcher_script(root, list(phases), int(time.time()) + deadline_in, "t-run"))
    took = time.monotonic() - t0
    assert rc == 0, "launcher did not start"
    # the launch must not wait for the job. Written as `cd X && nohup … &` it waited for the
    # whole job while holding the caller's stdout pipe, which would make `--detach` a lie.
    assert took < 5, f"launching took {took:.1f}s: the launch is not detaching from the job"


def _wait(remote: LocalRemote, *, timeout_s: float = 30):
    """Poll status.json the way the CLI does, and return the final RemoteJob."""
    end = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < end:
        lines: list[str] = []
        remote.run(status_script(remote.root), on_line=lines.append)
        last = parse_status(lines)
        if last.finished or last.died:
            return last
        time.sleep(0.2)
    raise AssertionError(f"job never finished; last status {last}")


def test_launcher_detaches_and_records_success(tmp_path: Path):
    remote = LocalRemote(tmp_path)
    _launch(remote, {"setup": "echo setting-up\n", "command": "echo working > $GPUOPS_OUT/r.txt\n"})
    job = _wait(remote)
    assert job.reported_phase == "finished"
    assert job.exit_code == 0
    assert job.pid is not None and job.started_at and job.finished_at
    log = (Path(remote.root) / "job.log").read_text()
    assert "setting-up" in log
    # the phase markers `gpu-ops logs --phase` splits on are written by the launcher
    assert "[gpu-ops] phase: setup" in log and "[gpu-ops] phase: command" in log
    assert (Path(remote.root) / "out" / "r.txt").read_text().strip() == "working"


def test_failing_phase_stops_the_run_and_keeps_its_code(tmp_path: Path):
    remote = LocalRemote(tmp_path)
    _launch(remote, {"setup": "echo boom; exit 3\n", "command": "echo should-not-run\n"})
    job = _wait(remote)
    assert job.reported_phase == "finished"
    assert job.exit_code == 3
    log = (Path(remote.root) / "job.log").read_text()
    assert "boom" in log and "should-not-run" not in log


def test_deadline_kills_the_job_with_124(tmp_path: Path):
    remote = LocalRemote(tmp_path)
    # the collection window already opens one second from now, so budget() is ~1s
    _launch(remote, {"command": "sleep 30\n"}, deadline_in=1)
    job = _wait(remote)
    assert job.exit_code == 124, "a job past its deadline must report coreutils' timeout code"


def test_secrets_reach_the_job_and_the_env_file_is_removed(tmp_path: Path):
    remote = LocalRemote(tmp_path)
    secret = "s3cr3t-value-123"
    _launch(remote, {"command": 'printf "%s" "$TOKEN" | wc -c > $GPUOPS_OUT/n.txt\n'},
            exports={"TOKEN": secret})
    job = _wait(remote)
    assert job.exit_code == 0
    assert (Path(remote.root) / "out" / "n.txt").read_text().strip() == str(len(secret))
    # the file the secret arrived in is gone; the value lived on only in the job's env
    assert not (Path(remote.root) / ".job_env").exists()
    assert secret not in (Path(remote.root) / "job.log").read_text()


def test_a_killed_job_reads_as_died_not_running(tmp_path: Path):
    remote = LocalRemote(tmp_path)
    _launch(remote, {"command": "sleep 300\n"})
    # wait until it is genuinely in the command phase, then kill the launcher outright,
    # the way an OOM kill or a container restart would
    for _ in range(100):
        lines: list[str] = []
        remote.run(status_script(remote.root), on_line=lines.append)
        job = parse_status(lines)
        if job.phase == "command" and job.pid:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("job never reached the command phase")
    remote.run(f"kill -9 {job.pid}\n")
    job = _wait(remote)
    assert job.died and job.reported_phase == "died"
    assert job.exit_code is None, "a job that died never got to write an exit code"


def test_parse_status_tolerates_rubbish():
    """A status read happens while money is being spent: it answers, it never raises."""
    assert parse_status([]).reported_phase == "unknown"
    assert parse_status(["not json at all", "GPUOPS_ALIVE=false"]).reported_phase == "unknown"
    job = parse_status(['{"phase":"command","pid":7,"exit_code":null,"finished_at":null}',
                        "GPUOPS_ALIVE=true", "GPUOPS_LASTLOG=epoch 3 loss 0.9"])
    assert job.phase == "command" and job.pid == 7 and job.alive
    assert job.last_log_line == "epoch 3 loss 0.9"
    assert not job.finished and not job.died
