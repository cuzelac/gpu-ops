from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

DIGEST_REF = "docker.io/library/debian@sha256:" + "a" * 64

POLICY = """\
[limits]
max_job_usd = 5.0
max_month_usd = 50.0
max_job_minutes = 360
max_concurrent = 2
allow_unarmed_deadman_minutes = 120

[providers]
allowed = ["fake"]
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated config/state dirs, fake provider enabled, a policy that allows only `fake`."""
    cfg, state, fake = tmp_path / "cfg", tmp_path / "state", tmp_path / "fake"
    cfg.mkdir()
    (cfg / "policy.toml").write_text(POLICY)
    monkeypatch.setenv("GPUOPS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("GPUOPS_STATE_DIR", str(state))
    monkeypatch.setenv("GPUOPS_FAKE_ROOT", str(fake))
    monkeypatch.setenv("GPUOPS_ENABLE_FAKE", "1")
    return tmp_path


def write_spec(dir: Path, *, command: str = "echo hi > $GPUOPS_OUT/result.txt", setup: str | None = None,
               outputs: str = '[[outputs]]\npath = "result.txt"\n', limits: str = "max_minutes = 5\nmax_cost_usd = 1.0",
               gpu: str = "min_vram_gb = 16", extra: str = "", inputs: bool = True) -> Path:
    data = dir / "data"
    data.mkdir(exist_ok=True)
    (data / "a.txt").write_text("alpha\n")
    (data / "sub").mkdir(exist_ok=True)
    (data / "sub" / "b.txt").write_text("beta\n")
    setup_line = f"setup = [{setup!r}]".replace("'", '"') if setup else ""
    inputs_block = '[[inputs]]\nname = "data"\nfrom = "data"\n' if inputs else ""
    spec = dir / "job.toml"
    spec.write_text(textwrap.dedent(f"""\
        name = "t-job"
        project = "tests"
        kind = "other"
        {extra}
        [image]
        ref = "{DIGEST_REF}"

        [run]
        {setup_line}
        command = '''{command}'''

        {inputs_block}
        {outputs}
        [gpu]
        {gpu}

        [limits]
        {limits}
        """))
    return spec
