"""The compact result shape, `--get` field selection and job-log filtering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode, GpuOpsError  # noqa: F401
from gpu_ops.result import PHASE_MARKER, compact, read_log, result, select, tail_lines

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
    res = result(0, **compact(RECORD, tmp_path, ["result.txt"]))
    assert res["ok"] is True and res["exit_code"] == 0
    assert res["gpu"] == "RTX A5000" and res["minutes"] == 0.9
    assert res["outputs"] == {"result.txt": 6}
    assert res["outputs_dir"] == str(out)
    # the key set never varies: report is null when the spec asked for none, log_tail when ok
    assert res["report"] is None and res["log_tail"] is None
    assert "a" * 64 not in str(res) and "inputs_file_count" not in res

    import json
    assert len(json.dumps(res, indent=2).splitlines()) <= 25


def test_result_supplies_ok_and_exit_code():
    """Every command's result has them, because one function puts them there."""
    assert result(0, a=1) == {"ok": True, "exit_code": 0, "a": 1}
    assert result(ExitCode.TEARDOWN_FAILED)["ok"] is False
    assert result(ExitCode.TEARDOWN_FAILED)["exit_code"] == 10


def test_compact_names_multiple_gpus(tmp_path: Path):
    assert compact({**RECORD, "gpu_count": 2}, tmp_path, [])["gpu"] == "RTX A5000 x2"


def test_compact_on_failure_carries_a_log_tail(tmp_path: Path):
    (tmp_path / "job.log").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    res = compact({**RECORD, "exit_code": 6, "status": "failed",
                   "error": "command exited with code 3"}, tmp_path, [])
    assert res["error"] == "command exited with code 3"
    assert res["log_tail"][0] == "line 30" and len(res["log_tail"]) == 20


def test_compact_report_is_null_until_a_spec_asks_for_one(tmp_path: Path):
    assert compact(RECORD, tmp_path, [])["report"] is None
    res = compact({**RECORD, "report": {"m.json": {"loss": 0.4}}}, tmp_path, [])
    assert res["report"] == {"m.json": {"loss": 0.4}}


def test_compact_collapses_a_directory_output(tmp_path: Path):
    """A declared directory output is one entry, however many files it holds."""
    out = tmp_path / "out" / "lora"
    out.mkdir(parents=True)
    for i in range(50):
        (out / f"shard-{i}.bin").write_text("x" * 10)
    import json
    manifest = {f"lora/shard-{i}.bin": "h" for i in range(50)}
    res = result(0, **compact({**RECORD, "outputs_manifest": manifest}, tmp_path, ["lora"]))
    assert res["outputs"] == {"lora": {"bytes": 500, "files": 50}}
    assert len(json.dumps(res, indent=2).splitlines()) <= 25

    # the property that matters: the result's size does not grow with the job's output
    huge = {f"lora/shard-{i}.bin": "h" for i in range(5000)}
    res_huge = result(0, **compact({**RECORD, "outputs_manifest": huge}, tmp_path, ["lora"]))
    assert len(json.dumps(res_huge, indent=2).splitlines()) == len(json.dumps(res, indent=2).splitlines())


def test_compact_skips_an_optional_output_the_job_never_wrote(tmp_path: Path):
    res = compact(RECORD, tmp_path, ["result.txt", "maybe.json"])
    assert list(res["outputs"]) == ["result.txt"]


def test_compact_survives_a_run_that_never_started_without_a_spec(tmp_path: Path):
    res = compact({"run_id": "x", "status": "failed", "exit_code": 4, "error": "no offer"}, tmp_path, [])
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


def test_select_backtracks_to_a_shorter_key():
    """Longest-prefix-wins must fall back, or a path that exists reads as missing."""
    obj = {"a": {"b": {"c": 1}}, "a.b": {"z": 9}}
    assert select(obj, "a.b.c") == {"a.b.c": 1}
    assert select(obj, "a.b.z") == {"a.b.z": 9}


def test_select_error_names_the_deepest_branch_not_the_last_tried():
    """The search backtracks, so its failure context must travel with it."""
    obj = {"a": {"x": 1}, "a.b": {"c": {"y": 1}}}
    with pytest.raises(GpuOpsError) as e:
        select(obj, "a.b.c.z")
    assert "at 'a.b.c'" in e.value.message and "available: y" in e.value.message


def test_select_error_on_a_list_names_the_list():
    with pytest.raises(GpuOpsError) as e:
        select({"instances": [{"id": "a"}]}, "instances.9.id")
    assert "at 'instances'" in e.value.message and "indexes 0..0" in e.value.message


def test_select_rejects_a_bogus_list_index_without_crashing():
    """`--1` passed an isdigit() check and reached int(), which raised a bare ValueError."""
    with pytest.raises(GpuOpsError) as e:
        select(RESULT, "instances.--1.id")
    assert e.value.code == ExitCode.SPEC_INVALID


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


def test_read_log_rejects_a_negative_tail(tmp_path: Path):
    p = tmp_path / "job.log"
    p.write_text(LOG)
    with pytest.raises(GpuOpsError) as e:
        read_log(p, tail=-5)  # an easy slip for --tail 5; it used to return the whole file
    assert e.value.code == ExitCode.SPEC_INVALID


def test_read_log_rejects_a_bad_regex(tmp_path: Path):
    p = tmp_path / "job.log"
    p.write_text(LOG)
    with pytest.raises(GpuOpsError) as e:
        read_log(p, grep="(unclosed")
    assert e.value.code == ExitCode.SPEC_INVALID


# ---------------- the _finish seam ----------------

class _Args:
    """The two attributes _finish and _emit read."""
    json = True
    get = None


def test_finish_returns_the_code_its_result_carries(capsys):
    from gpu_ops.cli import _finish
    assert _finish(_Args(), lambda: result(ExitCode.TEARDOWN_FAILED, results=[]), lambda r: "") == 10
    assert _finish(_Args(), lambda: result(0, a=1), lambda r: "") == 0


def test_finish_reports_the_fallback_when_the_work_itself_fails(capsys):
    """kill and reap have already terminated instances, so the conservative code is 10."""
    from gpu_ops.cli import _finish

    def work() -> dict:
        raise RuntimeError("provider returned nonsense")

    assert _finish(_Args(), work, lambda r: "", fallback=int(ExitCode.TEARDOWN_FAILED)) == 10
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 10 and "provider returned nonsense" in out["error"]


def test_finish_lets_a_deliberate_error_keep_its_own_code():
    """A GpuOpsError is an outcome with an exit code; the fallback must not swallow it."""
    from gpu_ops.cli import _finish

    def work() -> dict:
        raise GpuOpsError(ExitCode.SPEC_INVALID, "bad spec")

    with pytest.raises(GpuOpsError) as e:
        _finish(_Args(), work, lambda r: "", fallback=int(ExitCode.TEARDOWN_FAILED))
    assert e.value.code == ExitCode.SPEC_INVALID


def test_finish_keeps_a_clean_code_when_printing_breaks(capsys):
    """A formatting bug of ours must not turn a successful run into a failure."""
    from gpu_ops.cli import _finish

    def human(_r: dict) -> str:
        raise KeyError("a field the formatter assumed")

    args = _Args()
    args.json = False
    assert _finish(args, lambda: result(0, a=1), human) == 0
    assert json.loads(capsys.readouterr().out)["a"] == 1  # the result is still printed
