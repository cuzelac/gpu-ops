"""Directory entries in [[inputs]] include (bug reported 2026-11-17: they uploaded nothing, exit 7)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gpu_ops.errors import ExitCode
from gpu_ops.remote import LocalRemote, rsync_put_args

from .test_runner import assert_nothing_alive, run_job


def _tree(root: Path) -> None:
    (root / "games" / "sub").mkdir(parents=True)
    (root / "meta").mkdir()
    (root / "config.json").write_text("{}")
    (root / "games" / "g1.json").write_text("1")
    (root / "games" / "sub" / "g2.json").write_text("2")
    (root / "meta" / "m.json").write_text("m")
    (root / "excluded.txt").write_text("not listed")


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_rsync_include_recurses_into_directories(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src)
    dst.mkdir()
    args, stdin = rsync_put_args(src, f"{dst}/", ["config.json", "games", "meta"])
    r = subprocess.run(["rsync", "-a", "--protect-args", *args], input=stdin, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _files(dst) == {"config.json", "games/g1.json", "games/sub/g2.json", "meta/m.json"}


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
def test_without_r_rsync_drops_directory_contents(tmp_path: Path):
    """Pins the rsync behaviour the fix depends on, so a refactor that drops -r fails loudly."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _tree(src)
    dst.mkdir()
    r = subprocess.run(["rsync", "-a", "--protect-args", "--files-from=-", f"{src}/", f"{dst}/"],
                       input="config.json\ngames\n", capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _files(dst) == {"config.json"}


def test_run_with_directory_include(env: Path):
    code, rec, prov, runner = run_job(
        env, inputs=False,
        extra='[[inputs]]\nname = "d"\nfrom = "data"\ninclude = ["a.txt", "sub"]\n',
        command="cat $GPUOPS_IN/d/sub/b.txt > $GPUOPS_OUT/result.txt")
    assert code == ExitCode.OK, rec.error
    assert set(runner.inputs_manifest) == {"d/a.txt", "d/sub/b.txt"}  # off the record now
    assert (runner.run_dir / "out" / "result.txt").read_text() == "beta\n"
    assert_nothing_alive(prov)


class DroppingRemote(LocalRemote):
    """Uploads only plain files from an include list: the old bug's behaviour."""

    def put(self, local, remote_rel, include=None):
        super().put(local, remote_rel, [i for i in (include or []) if (local / i).is_file()] or None)


def test_upload_mismatch_says_file_did_not_arrive(env: Path):
    code, rec, prov, _ = run_job(
        env, remote_cls=DroppingRemote, inputs=False,
        extra='[[inputs]]\nname = "d"\nfrom = "data"\ninclude = ["a.txt", "sub"]\n')
    assert code == ExitCode.TRANSFER_FAILED
    assert "did not arrive on the machine: d/sub/b.txt" in rec.error
    assert "missing locally" not in rec.error
    assert_nothing_alive(prov)
