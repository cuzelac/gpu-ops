"""Secrets: read from 1Password at run time, held in memory only, redacted from all output.

op binary, first found of:
  1. $GPUOPS_OP_BIN
  2. op.exe on PATH — the Windows CLI, reached from WSL through the Windows PATH; it unlocks through the
     1Password desktop app. Preferred over a Linux `op`, which on this machine is not signed in.
  3. op on PATH
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .errors import ExitCode, GpuOpsError


def op_binary() -> str:
    if v := os.environ.get("GPUOPS_OP_BIN"):
        return v
    for name in ("op.exe", "op"):
        if found := shutil.which(name):
            return found
    raise GpuOpsError(ExitCode.REFUSED, "1Password CLI not found (looked for op.exe and op on PATH); "
                                        "set GPUOPS_OP_BIN")


class Redactor:
    """Replaces every known secret value with *** in any text it sees."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str) -> None:
        if value and len(value) >= 4:
            self._values.add(value)

    def __call__(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            text = text.replace(v, "***")
        return text


def read_op(ref: str, redactor: Redactor | None = None) -> str:
    if not ref.startswith("op://"):
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"not a 1Password reference: {ref!r}")
    try:
        r = subprocess.run([op_binary(), "read", "--no-newline", ref], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise GpuOpsError(ExitCode.REFUSED, f"1Password did not answer within 120 s for {ref} (approval prompt pending?)")
    if r.returncode != 0:
        msg = (r.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise GpuOpsError(ExitCode.REFUSED, f"could not read {ref} from 1Password: {msg[0]}")
    value = r.stdout.rstrip("\r\n")
    if redactor is not None:
        redactor.add(value)
    return value
