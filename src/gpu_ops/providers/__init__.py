"""Provider registry. To add a provider: write providers/<name>.py and add a branch to `load_providers`."""

from __future__ import annotations

import os

from ..errors import ExitCode, GpuOpsError
from ..policy import load_provider_settings
from ..secrets import Redactor, read_op
from .base import Provider

KNOWN = ("runpod", "fake")


def load_providers(names: list[str], redactor: Redactor) -> dict[str, Provider]:
    """Instantiate the named providers that are configured. Unknown or unconfigured ones are skipped."""
    settings = load_provider_settings()
    out: dict[str, Provider] = {}
    for n in names:
        if n == "runpod":
            ref = (settings.get("runpod") or {}).get("api_key_ref")
            if not ref:
                raise GpuOpsError(ExitCode.REFUSED, "runpod: no api_key_ref in providers.toml (run `gpu-ops init`)")
            from .runpod import RunPodProvider
            out[n] = RunPodProvider(read_op(ref, redactor))
        elif n == "fake":
            if os.environ.get("GPUOPS_ENABLE_FAKE") == "1":
                from .fake import FakeProvider
                out[n] = FakeProvider()
    return out
