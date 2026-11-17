"""The compact result shape, `--get` field selection and job-log filtering."""

from __future__ import annotations

from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode, GpuOpsError
from gpu_ops.result import PHASE_MARKER, compact, read_log, select, tail_lines

RECORD = {
    "run_id": "20261116T120000-abcd", "status": "ok", "exit_code": 0, "error": None,
    "gpu_name": "RTX A5000", "gpu_count": 1, "tier": "community", "price_per_hr": 0.16,
    "billed_seconds": 54.0, "cost_estimate_usd": 0.0103, "teardown_confirmed": True,
    "outputs_verified": True, "deadman": "armed", "notes": [],
    "outputs_manifest": {"result.txt": "a" * 64},
    "inputs_hash": "b" * 64, "inputs_file_count": 2, "phases": {"command": 1.0},
}


def test_compact_is_short_and_has_no_hashes(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "result.txt").write_text("alpha\n")
    res = compact(RECORD, tmp_path)
    assert res["ok"] is True and res["exit_code"] == 0
    assert res["gpu"] == "RTX A5000" and res["minutes"] == 0.9
    assert res["outputs"] == {"result.txt": 6}
    assert res["outputs_dir"] == str(out)
    assert "report" not in res and "log_tail" not in res  # neither asked for, nothing failed
    assert "a" * 64 not in str(res) and "inputs_file_count" not in res

    import json
    assert len(json.dumps(res, indent=2).splitlines()) <= 25


def test_compact_names_multiple_gpus(tmp_path: Path):
    assert compact({**RECORD, "gpu_count": 2}, tmp_path)["gpu"] == "RTX A5000 x2"


def test_compact_on_failure_carries_a_log_tail(tmp_path: Path):
    (tmp_path / "job.log").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    res = compact({**RECORD, "exit_code": 6, "status": "failed", "error": "command exited with code 3"}, tmp_path)
    assert res["ok"] is False and res["error"] == "command exited with code 3"
    assert res["log_tail"][0] == "line 30" and len(res["log_tail"]) == 20


def test_compact_includes_report_only_when_asked_for(tmp_path: Path):
    assert "report" not in compact(RECORD, tmp_path)
    res = compact({**RECORD, "report": {"m.json": {"loss": 0.4}}}, tmp_path)
    assert res["report"] == {"m.json": {"loss": 0.4}}


def test_compact_survives_a_run_that_never_started(tmp_path: Path):
    res = compact({"run_id": "x", "status": "failed", "exit_code": 4, "error": "no offer"}, tmp_path)
    assert res["gpu"] == "?" and res["minutes"] is None and res["outputs"] == {}


def test_tail_lines_without_a_file(tmp_path: Path):
    assert tail_lines(tmp_path / "nope.log", 5) == []


# ---------------- --get ----------------

RESULT = {"ok": True, "plan": {"offer": {"price_per_hr": 0.16}, "worst_case_usd": 0.05, "notes": []},
          "instances": [{"id": "a1"}, {"id": "b2"}],
          "report": {"lora/train-record.json": {"eval_loss": 0.42}}}


def test_select_reads_nested_paths():
    got = select(RESULT, "plan.offer.price_per_hr,plan.worst_case_usd")
    assert got == {"plan.offer.price_per_hr": 0.16, "plan.worst_case_usd": 0.05}


def test_select_indexes_lists_and_whole_keys_with_dots():
    assert select(RESULT, "instances.1.id") == {"instances.1.id": "b2"}
    assert select(RESULT, " ok ") == {"ok": True}
    # a report/output key contains dots and slashes: it matches as a whole
    assert select(RESULT, "report.lora/train-record.json")["report.lora/train-record.json"] == {"eval_loss": 0.42}
    assert select(RESULT, "report.lora/train-record.json.eval_loss")[
        "report.lora/train-record.json.eval_loss"] == 0.42


@pytest.mark.parametrize("path", ["plan.offer.nope", "nope", "instances.9.id", "instances.x", "ok.nope", ""])
def test_select_rejects_a_path_that_is_not_there(path):
    with pytest.raises(GpuOpsError) as e:
        select(RESULT, path)
    assert e.value.code == ExitCode.SPEC_INVALID


def test_select_error_lists_what_is_available():
    with pytest.raises(GpuOpsError) as e:
        select(RESULT, "plan.offerx")
    assert "price_per_hr" not in e.value.message and "worst_case_usd" in e.value.message


# ---------------- logs ----------------

LOG = "\n".join([
    PHASE_MARKER + "setup",
    "Collecting torch",
    "ERROR: pip is confused",
    PHASE_MARKER + "command",
    "epoch 1",
    "ERROR: cuda oom",
    "epoch 2",
]) + "\n"


def test_read_log_tail_and_grep(tmp_path: Path):
    p = tmp_path / "job.log"
    p.write_text(LOG)
    lines, in_file, matched = read_log(p, tail=1, grep="ERROR")
    assert lines == ["ERROR: cuda oom"] and matched == 2 and in_file == 7
    lines, _, _ = read_log(p, tail=0)
    assert len(lines) == 7


def test_read_log_by_phase(tmp_path: Path):
    p = tmp_path / "job.log"
    p.write_text(LOG)
    assert read_log(p, phase="setup")[0] == ["Collecting torch", "ERROR: pip is confused"]
    assert read_log(p, phase="command")[0] == ["epoch 1", "ERROR: cuda oom", "epoch 2"]
    assert read_log(p, phase="command", grep="^epoch")[0] == ["epoch 1", "epoch 2"]


def test_read_log_rejects_a_bad_regex(tmp_path: Path):
    p = tmp_path / "job.log"
    p.write_text(LOG)
    with pytest.raises(GpuOpsError) as e:
        read_log(p, grep="(unclosed")
    assert e.value.code == ExitCode.SPEC_INVALID
