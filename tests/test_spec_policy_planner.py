from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode, GpuOpsError
from gpu_ops.names import instance_name, new_run_id, parse_instance_name
from gpu_ops.paths import to_local_path
from gpu_ops.planner import filter_offers, make_plan, month_spend
from gpu_ops.policy import load_policy
from gpu_ops.providers.base import Offer
from gpu_ops.providers.fake import FakeProvider
from gpu_ops.spec import GpuSpec, load_spec

from .conftest import write_spec


def test_valid_spec_loads(env: Path):
    loaded = load_spec(write_spec(env))
    assert loaded.spec.name == "t-job"
    assert loaded.input_paths["data"] == env / "data"


@pytest.mark.parametrize("bad, fragment", [
    ('[image]\nref = "debian:12"', "pinned by digest"),
    ("unknown_field = 1", "Extra inputs"),
])
def test_invalid_specs_rejected(env: Path, bad: str, fragment: str):
    spec = write_spec(env)
    text = spec.read_text()
    if bad.startswith("[image]"):
        text = text.replace(text[text.index("[image]"):text.index("[run]")], bad + "\n\n")
    else:
        text = bad + "\n" + text
    spec.write_text(text)
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert e.value.code == ExitCode.SPEC_INVALID and fragment in e.value.message


def test_limits_are_required(env: Path):
    spec = write_spec(env, limits="max_minutes = 5")
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert "max_cost_usd" in e.value.message


def test_literal_secret_rejected(env: Path):
    spec = write_spec(env, outputs='[[outputs]]\npath = "result.txt"\n\n[secrets]\nHF_TOKEN = "hf_abc"\n')
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert "1Password reference" in e.value.message


def test_windows_path_becomes_mnt(tmp_path: Path):
    assert to_local_path("C:/Users/x/data", tmp_path) == Path("/mnt/c/Users/x/data")
    assert to_local_path(r"D:\models\m", tmp_path) == Path("/mnt/d/models/m")


def test_name_roundtrip():
    rid = new_run_id(datetime(2026, 11, 11, 18, 30, tzinfo=timezone.utc))
    name = instance_name(rid, 1762890000, "hs-qlora-v1b")
    p = parse_instance_name(name)
    assert p and p.run_id == rid and p.deadline_epoch == 1762890000 and p.job_name == "hs-qlora-v1b"
    assert parse_instance_name("my-manual-pod") is None
    assert parse_instance_name("gpuops-garbage") is None


def _offer(gid, vram, tier, price, avail=3, count=1):
    return Offer("fake", gid, gid, vram, count, tier, price, avail)


def test_filter_skips_unavailable_and_respects_constraints():
    offers = [_offer("A", 24, "community", 0.2), _offer("B", 48, "secure", 0.5), _offer("C", 80, "secure", 0.01, 0),
              _offer("D", 16, "community", 0.1)]
    got = filter_offers(offers, GpuSpec(min_vram_gb=24))
    assert [o.gpu_id for o in got] == ["A", "B"]  # C has no availability, D too small
    assert [o.gpu_id for o in filter_offers(offers, GpuSpec(min_vram_gb=24, tier="secure"))] == ["B"]
    assert [o.gpu_id for o in filter_offers(offers, GpuSpec(min_vram_gb=24, max_price_per_hr=0.3))] == ["A"]


def test_plan_picks_cheapest_and_computes_worst_case(env: Path):
    loaded = load_spec(write_spec(env, limits="max_minutes = 60\nmax_cost_usd = 1.0"))
    plan = make_plan(loaded.spec, load_policy(), {"fake": FakeProvider()}, [])
    assert plan.offer.gpu_id == "FAKE-24GB" and plan.offer.tier == "community"
    assert plan.run_minutes == 60 and plan.worst_case_usd == pytest.approx(0.20)


def test_cost_cap_shortens_deadline(env: Path):
    loaded = load_spec(write_spec(env, limits="max_minutes = 600\nmax_cost_usd = 0.10"))
    policy = load_policy()
    policy.limits.max_job_minutes = 1000
    plan = make_plan(loaded.spec, policy, {"fake": FakeProvider()}, [])
    assert plan.run_minutes == pytest.approx(30)  # $0.10 at $0.20/hr
    assert any("deadline shortened" in n for n in plan.notes)


def test_policy_per_job_refusal(env: Path):
    loaded = load_spec(write_spec(env, gpu="min_vram_gb = 80", limits="max_minutes = 300\nmax_cost_usd = 10"))
    with pytest.raises(GpuOpsError) as e:
        make_plan(loaded.spec, load_policy(), {"fake": FakeProvider()}, [])
    assert e.value.code == ExitCode.REFUSED and "max_job_usd" in e.value.message


def test_policy_monthly_refusal(env: Path):
    loaded = load_spec(write_spec(env))
    now = datetime.now(timezone.utc)
    records = [{"started_at": now.isoformat(), "cost_estimate_usd": 49.99}]
    assert month_spend(records, now) == pytest.approx(49.99)
    with pytest.raises(GpuOpsError) as e:
        make_plan(loaded.spec, load_policy(), {"fake": FakeProvider()}, records)
    assert e.value.code == ExitCode.REFUSED and "max_month_usd" in e.value.message


def test_no_offer(env: Path):
    loaded = load_spec(write_spec(env, gpu="min_vram_gb = 200"))
    with pytest.raises(GpuOpsError) as e:
        make_plan(loaded.spec, load_policy(), {"fake": FakeProvider()}, [])
    assert e.value.code == ExitCode.NO_OFFER


def test_provider_not_allowed_by_policy(env: Path):
    loaded = load_spec(write_spec(env))
    policy = load_policy()
    policy.providers.allowed = ["runpod"]
    with pytest.raises(GpuOpsError) as e:
        make_plan(loaded.spec, policy, {"fake": FakeProvider()}, [])
    assert e.value.code == ExitCode.REFUSED


def test_select_fallback_note(env: Path):
    loaded = load_spec(write_spec(env, gpu='min_vram_gb = 16\nselect = "fastest"'))
    plan = make_plan(loaded.spec, load_policy(), {"fake": FakeProvider()}, [])
    assert any("fell back to cheapest-hour" in n for n in plan.notes)


def test_month_spend_counts_killed_runs_like_spend():
    now = datetime(2026, 11, 20, tzinfo=timezone.utc)
    killed = {"started_at": "2026-11-12T14:13:24+00:00", "deadline": "2026-11-12T14:16:24+00:00",
              "instance_id": "k", "price_per_hr": 0.20}
    finished = {"started_at": "2026-11-12T10:00:00+00:00", "ended_at": "2026-11-12T10:10:00+00:00",
                "instance_id": "f", "cost_estimate_usd": 0.05}
    assert month_spend([killed, finished], now) == pytest.approx(0.01 + 0.05, abs=1e-6)


def test_report_file_must_also_be_an_output(env: Path):
    spec = write_spec(env, extra='[report]\nfiles = ["metrics.json"]\n')
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert e.value.code == ExitCode.SPEC_INVALID and "must also be listed in [[outputs]]" in e.value.message


def test_report_max_bytes_is_capped(env: Path):
    spec = write_spec(env, outputs='[[outputs]]\npath = "result.txt"\n',
                      extra='[report]\nfiles = ["result.txt"]\nmax_bytes = 999999\n')
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert e.value.code == ExitCode.SPEC_INVALID


def test_report_defaults(env: Path):
    loaded = load_spec(write_spec(env, outputs='[[outputs]]\npath = "result.txt"\n',
                                  extra='[report]\nfiles = ["result.txt"]\n'))
    assert loaded.spec.report.max_bytes == 16384


def test_overlapping_outputs_rejected(env: Path):
    """Two outputs where one contains the other would be counted twice in the result."""
    spec = write_spec(env, outputs='[[outputs]]\npath = "lora"\n[[outputs]]\npath = "lora/adapter"\n')
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert "is inside output" in e.value.message


def test_duplicate_outputs_rejected(env: Path):
    spec = write_spec(env, outputs='[[outputs]]\npath = "a.txt"\n[[outputs]]\npath = "a.txt"\n')
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert "must be unique" in e.value.message


def test_report_budget_is_bounded_in_total_not_just_per_file(env: Path):
    """16 files x 64 KiB would be 1 MB inside a result advertised as compact."""
    files = ", ".join(f'"f{i}.txt"' for i in range(16))
    outs = "".join(f'[[outputs]]\npath = "f{i}.txt"\n' for i in range(16))
    spec = write_spec(env, outputs=outs, extra=f'[report]\nfiles = [{files}]\nmax_bytes = 65536\n')
    with pytest.raises(GpuOpsError) as e:
        load_spec(spec)
    assert "the limit is" in e.value.message


def test_report_within_budget_loads(env: Path):
    files = ", ".join(f'"f{i}.txt"' for i in range(4))
    outs = "".join(f'[[outputs]]\npath = "f{i}.txt"\n' for i in range(4))
    loaded = load_spec(write_spec(env, outputs=outs,
                                  extra=f'[report]\nfiles = [{files}]\nmax_bytes = 16384\n'))
    assert len(loaded.spec.report.files) == 4
