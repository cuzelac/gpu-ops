"""1Password reads: service account first (no prompt), desktop app as fallback. Uses fake `op` scripts."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from gpu_ops import secrets as S
from gpu_ops.errors import ExitCode, GpuOpsError

TOKEN = "ops_FAKE-service-account-token-1234567890"


def fake_op(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + f'echo "$@" >> {path}.argv\n' + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def ops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sa = fake_op(tmp_path / "op-sa", f'[ "$OP_SERVICE_ACCOUNT_TOKEN" = "{TOKEN}" ] || exit 1\nprintf sa-value\n')
    desk = fake_op(tmp_path / "op-desk", "printf desktop-value\n")
    monkeypatch.setenv("GPUOPS_OP_SA_BIN", str(sa))
    monkeypatch.setenv("GPUOPS_OP_BIN", str(desk))
    monkeypatch.delenv("GPUOPS_NO_SERVICE_ACCOUNT", raising=False)
    monkeypatch.setattr(S, "_sa_cache", S._UNSET)
    return sa, desk


def test_service_account_used_and_token_never_in_argv(ops, monkeypatch, capsys):
    sa, desk = ops
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", TOKEN)
    red = S.Redactor()
    assert S.read_op("op://v/i/f", red) == "sa-value"
    assert TOKEN not in Path(f"{sa}.argv").read_text()
    assert not Path(f"{desk}.argv").exists()  # no desktop prompt
    assert red(f"x {TOKEN} y") == "x *** y" and red("sa-value") == "***"


def test_bad_token_falls_back_to_desktop_without_leaking(ops, monkeypatch, capsys):
    sa, desk = ops
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_wrong-token-value")
    assert S.read_op("op://v/i/f") == "desktop-value"
    err = capsys.readouterr().err
    assert "falling back to the 1Password app" in err and "ops_wrong-token-value" not in err


def test_opt_out_uses_desktop_only(ops, monkeypatch):
    sa, desk = ops
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", TOKEN)
    monkeypatch.setenv("GPUOPS_NO_SERVICE_ACCOUNT", "1")
    assert S.read_op("op://v/i/f") == "desktop-value"
    assert not Path(f"{sa}.argv").exists()


def test_no_token_anywhere_uses_desktop(ops, monkeypatch):
    sa, desk = ops
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setattr(S, "_load_service_account_token", lambda: None)
    assert S.read_op("op://v/i/f") == "desktop-value"


def test_desktop_failure_is_exit_3(tmp_path, monkeypatch):
    desk = fake_op(tmp_path / "op-desk", 'echo "[ERROR] authorization timeout" >&2; exit 1\n')
    monkeypatch.setenv("GPUOPS_OP_BIN", str(desk))
    monkeypatch.setenv("GPUOPS_NO_SERVICE_ACCOUNT", "1")
    monkeypatch.setattr(S, "_sa_cache", S._UNSET)
    with pytest.raises(GpuOpsError) as e:
        S.read_op("op://v/i/f")
    assert e.value.code == ExitCode.REFUSED and "authorization timeout" in e.value.message


def test_no_fallback_fails_instead_of_prompting(ops, monkeypatch):
    sa, desk = ops
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_wrong-token-value")
    monkeypatch.setenv("GPUOPS_NO_DESKTOP_FALLBACK", "1")
    with pytest.raises(GpuOpsError) as e:
        S.read_op("op://v/i/f")
    assert e.value.code == ExitCode.REFUSED and "fallback is off" in e.value.message
    assert "ops_wrong-token-value" not in e.value.message
    assert not Path(f"{desk}.argv").exists()  # the desktop app was never asked


def test_no_fallback_without_token_fails(ops, monkeypatch):
    sa, desk = ops
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setattr(S, "_load_service_account_token", lambda: None)
    monkeypatch.setenv("GPUOPS_NO_DESKTOP_FALLBACK", "1")
    with pytest.raises(GpuOpsError) as e:
        S.read_op("op://v/i/f")
    assert e.value.code == ExitCode.REFUSED and "no 1Password service account token" in e.value.message
    assert not Path(f"{desk}.argv").exists()


def test_session_failure_mentions_clock(tmp_path, monkeypatch):
    msg = "[ERROR] could not read secret: error initializing client: failed to re-initialize service account session"
    sa = fake_op(tmp_path / "op-sa", f'echo "{msg}" >&2; exit 1\n')
    monkeypatch.setenv("GPUOPS_OP_SA_BIN", str(sa))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", TOKEN)
    monkeypatch.setenv("GPUOPS_NO_DESKTOP_FALLBACK", "1")
    monkeypatch.delenv("GPUOPS_NO_SERVICE_ACCOUNT", raising=False)
    monkeypatch.setattr(S, "_sa_cache", S._UNSET)
    with pytest.raises(GpuOpsError) as e:
        S.read_op("op://v/i/f")
    assert "check the system clock" in e.value.message
