"""Where gpu-ops keeps its files, and Windows-path handling.

- Config (policy, provider settings, speed table): ~/.config/gpu-ops   (override: GPUOPS_CONFIG_DIR)
- State (ledger, run records, job logs, outputs): ~/.local/state/gpu-ops (override: GPUOPS_STATE_DIR)

Both live outside any project repo on purpose: a job spec can never point gpu-ops
at a looser policy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_WIN_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def config_dir() -> Path:
    if v := os.environ.get("GPUOPS_CONFIG_DIR"):
        return Path(v)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gpu-ops"


def state_dir() -> Path:
    if v := os.environ.get("GPUOPS_STATE_DIR"):
        return Path(v)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "gpu-ops"


def to_local_path(raw: str, base: Path) -> Path:
    """Turn a path written in a job spec into an absolute local path.

    Windows paths (C:/x or C:\\x) become /mnt/c/x when running under Linux/WSL,
    which is how gpu-ops normally runs. Relative paths are relative to the spec file.
    """
    m = _WIN_DRIVE.match(raw)
    if m and os.name != "nt":
        drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    p = Path(raw)
    return p if p.is_absolute() else (base / p).resolve()
