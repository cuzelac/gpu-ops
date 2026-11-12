"""Running commands on, and moving files to/from, an instance.

SshRemote: system ssh + rsync (WSL). Scripts go over stdin, so secrets in them never
appear in any process's argv on either machine.
LocalRemote: same interface on a local directory, for tests and fake-provider demos.

Remote layout under `root`: in/<input-name>/, out/, work/.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

LineSink = Callable[[str], None]
TIMEOUT_EXIT = 124  # what coreutils `timeout` returns; we return it too when the local guard fires


class Remote(Protocol):
    root: str

    def wait_ready(self, timeout_s: float) -> bool: ...

    def run(self, script: str, *, timeout_s: float | None = None, on_line: LineSink | None = None) -> int: ...

    def put(self, local: Path, remote_rel: str, include: list[str] | None = None) -> None: ...

    def get(self, remote_rel: str, local: Path) -> None: ...


class TransferError(Exception):
    pass


def _stream(cmd: list[str], stdin_text: str, timeout_s: float | None, on_line: LineSink | None) -> int:
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    fired = threading.Event()

    def _kill() -> None:
        fired.set()
        proc.kill()

    timer = threading.Timer(timeout_s, _kill) if timeout_s else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(stdin_text)
        proc.stdin.close()
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip("\n"))
        rc = proc.wait()
    finally:
        if timer:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return TIMEOUT_EXIT if fired.is_set() else rc


def _with_remote_timeout(timeout_s: float | None) -> str:
    # The remote `timeout` kills the job on the instance itself; the local timer (a bit later)
    # only covers a hung connection.
    return f"timeout -k 30 {int(max(1, timeout_s))} bash -s" if timeout_s else "bash -s"


class SshRemote:
    def __init__(self, host: str, port: int, key_path: Path, user: str = "root", root: str = "/gpuops"):
        self.host, self.port, self.key_path, self.user, self.root = host, port, Path(key_path), user, root

    def _ssh_opts(self) -> list[str]:
        return ["-i", str(self.key_path), "-p", str(self.port),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]

    def _ssh(self, remote_cmd: str) -> list[str]:
        return ["ssh", *self._ssh_opts(), f"{self.user}@{self.host}", remote_cmd]

    def _rsync_e(self) -> str:
        return "ssh " + " ".join(shlex.quote(o) for o in self._ssh_opts())

    def wait_ready(self, timeout_s: float) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            r = subprocess.run(self._ssh("command -v rsync >/dev/null && test -d " + shlex.quote(self.root)),
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return True
            time.sleep(5)
        return False

    def run(self, script: str, *, timeout_s: float | None = None, on_line: LineSink | None = None) -> int:
        local_guard = timeout_s + 90 if timeout_s else None
        return _stream(self._ssh(_with_remote_timeout(timeout_s)), script, local_guard, on_line)

    def put(self, local: Path, remote_rel: str, include: list[str] | None = None) -> None:
        dest = f"{self.root}/{remote_rel}".rstrip("/")
        mk = subprocess.run(self._ssh(f"mkdir -p {shlex.quote(dest)}"), capture_output=True, text=True, timeout=60)
        if mk.returncode != 0:
            raise TransferError(f"mkdir {dest} failed: {mk.stderr.strip()}")
        base = ["rsync", "-a", "--protect-args", "-e", self._rsync_e()]
        if local.is_dir() and include:
            cmd = base + ["--files-from=-", f"{local}/", f"{self.user}@{self.host}:{dest}/"]
            stdin = "\n".join(include) + "\n"
        elif local.is_dir():
            cmd, stdin = base + [f"{local}/", f"{self.user}@{self.host}:{dest}/"], None
        else:
            cmd, stdin = base + [str(local), f"{self.user}@{self.host}:{dest}/"], None
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        if r.returncode != 0:
            raise TransferError(f"upload {local} failed (rsync exit {r.returncode}): {r.stderr.strip()[-500:]}")

    def get(self, remote_rel: str, local: Path) -> None:
        # -R with the /./ marker recreates remote_rel under `local`, for files and directories alike
        src = f"{self.user}@{self.host}:{self.root}/./{remote_rel}"
        local.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["rsync", "-a", "-R", "--protect-args", "-e", self._rsync_e(), src, f"{local}/"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise TransferError(f"download {remote_rel} failed (rsync exit {r.returncode}): {r.stderr.strip()[-500:]}")


class LocalRemote:
    """A directory standing in for an instance. Commands run with local bash."""

    def __init__(self, root: Path):
        self.root = str(Path(root).resolve())
        for d in ("in", "out", "work"):
            (Path(self.root) / d).mkdir(parents=True, exist_ok=True)

    def wait_ready(self, timeout_s: float) -> bool:
        return True

    def run(self, script: str, *, timeout_s: float | None = None, on_line: LineSink | None = None) -> int:
        cmd = ["bash", "-c", _with_remote_timeout(timeout_s)]
        return _stream(cmd, script, timeout_s + 30 if timeout_s else None, on_line)

    def put(self, local: Path, remote_rel: str, include: list[str] | None = None) -> None:
        dest = Path(self.root) / remote_rel
        dest.mkdir(parents=True, exist_ok=True)
        if local.is_dir() and include:
            for rel in include:
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local / rel, dest / rel)
        elif local.is_dir():
            shutil.copytree(local, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(local, dest / local.name)

    def get(self, remote_rel: str, local: Path) -> None:
        src = Path(self.root) / remote_rel
        dst = local / remote_rel
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            raise TransferError(f"download {remote_rel} failed: not found")
