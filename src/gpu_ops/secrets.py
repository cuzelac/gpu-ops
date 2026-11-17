"""Secrets: read from 1Password at run time, held in memory only, redacted from all output.

Two ways to read, tried in this order:

1. Service account (unattended; no prompt).
   The token comes from $OP_SERVICE_ACCOUNT_TOKEN if already set, otherwise from Windows Credential
   Manager via bin/gpuops-credential.ps1 (target "gpu-ops/op-service-account"; override with
   $GPUOPS_OP_SA_TARGET). It is passed to `op` only through that process's environment, never argv.
   op binary: $GPUOPS_OP_SA_BIN, else a Linux `op` on PATH, else op.exe with the token forwarded via WSLENV.
   Disable with GPUOPS_NO_SERVICE_ACCOUNT=1.

2. Desktop app (interactive; 1Password may ask you to approve).
   op binary: $GPUOPS_OP_BIN, else op.exe on PATH (the Windows CLI, which unlocks through the desktop app),
   else `op` on PATH.

If the service account read fails, gpu-ops says so on stderr and falls back to the desktop app.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import ExitCode, GpuOpsError

SA_TARGET_DEFAULT = "gpu-ops/op-service-account"
_UNSET = object()
_sa_cache: object | str | None = _UNSET


def op_binary() -> str:
    if v := os.environ.get("GPUOPS_OP_BIN"):
        return v
    for name in ("op.exe", "op"):
        if found := shutil.which(name):
            return found
    raise GpuOpsError(ExitCode.REFUSED, "1Password CLI not found (looked for op.exe and op on PATH); "
                                        "set GPUOPS_OP_BIN")


class Redactor:
    """Replaces every known secret value with *** in any text or parsed value it sees.

    Two limits, both deliberate and both the reason `scrub` exists:
    - A value shorter than 4 characters is not registered; it would match everywhere.
    - Matching is literal, so a secret only matches text that holds it verbatim. A secret
      spanning two lines never matches a line-by-line log, and one containing a quote or
      backslash does not match its own JSON-escaped form. So redact *parsed values* with
      `scrub` wherever the data is structured, and keep `__call__` for raw log lines.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str) -> None:
        if value and len(value) >= 4:
            self._values.add(value)

    def __call__(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, "***")
        return text

    def scrub(self, obj: Any) -> Any:
        """Redact every string inside a parsed structure, keys included.

        Applied at the boundaries where data leaves gpu-ops — the record on disk and the
        result on stdout — so no caller has to remember to redact its own field.
        """
        if isinstance(obj, str):
            return self(obj)
        if isinstance(obj, dict):
            return {(self(k) if isinstance(k, str) else k): self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj


def credential_script() -> Path:
    return Path(__file__).resolve().parents[2] / "bin" / "gpuops-credential.ps1"


def service_account_token() -> str | None:
    """The service account token, or None if none is configured. Cached for the life of the process."""
    global _sa_cache
    if _sa_cache is not _UNSET:
        return _sa_cache  # type: ignore[return-value]
    _sa_cache = _load_service_account_token()
    return _sa_cache  # type: ignore[return-value]


def _load_service_account_token() -> str | None:
    if os.environ.get("GPUOPS_NO_SERVICE_ACCOUNT") == "1":
        return None
    if v := os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        return v
    ps = shutil.which("powershell.exe")
    script = credential_script()
    if not ps or not script.exists():
        return None
    try:
        win = subprocess.run(["wslpath", "-w", str(script)], capture_output=True, text=True, timeout=10).stdout.strip()
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", win, "get",
                            "-Target", os.environ.get("GPUOPS_OP_SA_TARGET", SA_TARGET_DEFAULT)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = r.stdout.strip()
    return token if r.returncode == 0 and token.startswith("ops_") else None


def _sa_command(token: str) -> tuple[str, dict[str, str]]:
    env = {**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token}
    if v := os.environ.get("GPUOPS_OP_SA_BIN"):
        return v, env
    if linux_op := shutil.which("op"):
        return linux_op, env
    if win_op := shutil.which("op.exe"):
        # a Windows exe launched from WSL only sees variables listed in WSLENV
        env["WSLENV"] = ":".join(x for x in (os.environ.get("WSLENV"), "OP_SERVICE_ACCOUNT_TOKEN") if x)
        return win_op, env
    raise GpuOpsError(ExitCode.REFUSED, "1Password CLI not found for the service account (looked for op, op.exe)")


def _run_op(binary: str, ref: str, env: dict[str, str] | None, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([binary, "read", "--no-newline", ref], capture_output=True, text=True, timeout=timeout,
                          env=env)


def read_op(ref: str, redactor: Redactor | None = None) -> str:
    if not ref.startswith("op://"):
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"not a 1Password reference: {ref!r}")

    token = service_account_token()
    if token:
        if redactor is not None:
            redactor.add(token)
        binary, env = _sa_command(token)
        try:
            r = _run_op(binary, ref, env, timeout=60)
            if r.returncode == 0:
                return _accept(r.stdout, redactor)
            why = ((r.stderr or "").strip().splitlines() or ["unknown error"])[-1]
        except subprocess.TimeoutExpired:
            why = "timed out after 60 s"
        why = redactor(why) if redactor is not None else why.replace(token, "***")
        print(f"gpu-ops: service account could not read {ref} ({why}); falling back to the 1Password app",
              file=sys.stderr)

    try:
        r = _run_op(op_binary(), ref, None, timeout=120)
    except subprocess.TimeoutExpired:
        raise GpuOpsError(ExitCode.REFUSED, f"1Password did not answer within 120 s for {ref} (approval prompt pending?)")
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise GpuOpsError(ExitCode.REFUSED, f"could not read {ref} from 1Password: {msg[0]}")
    return _accept(r.stdout, redactor)


def _accept(stdout: str, redactor: Redactor | None) -> str:
    value = stdout.rstrip("\r\n")
    if redactor is not None:
        redactor.add(value)
    return value
