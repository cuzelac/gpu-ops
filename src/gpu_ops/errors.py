"""Exit codes and the one exception type the CLI turns into them.

The codes are a public contract (documented in AGENTS.md). Never renumber them.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INTERNAL = 1  # a bug in gpu-ops; not raised on purpose
    SPEC_INVALID = 2
    REFUSED = 3  # refused by policy or limits
    NO_OFFER = 4  # no offer or capacity
    PROVIDER_ERROR = 5  # provider/API error before the job ran
    JOB_FAILED = 6  # job exited non-zero, or required outputs missing
    TRANSFER_FAILED = 7  # rsync failed or checksums did not match
    CAP_HIT = 8  # time or cost cap reached; job killed
    TEARDOWN_FAILED = 10  # instance may still be billing
    # A detached run is still going: nothing was collected, and the machine is alive on
    # purpose. Non-zero deliberately — a caller reading only the code must not mistake this
    # for "finished, the outputs are home".
    STILL_RUNNING = 11


def worst(*codes: ExitCode) -> ExitCode:
    """Pick the exit code to report when several things went wrong.

    TEARDOWN_FAILED always wins, because money may still be leaving.
    Otherwise the first non-OK code wins (the earliest failure is the cause).
    """
    if ExitCode.TEARDOWN_FAILED in codes:
        return ExitCode.TEARDOWN_FAILED
    for c in codes:
        if c != ExitCode.OK:
            return c
    return ExitCode.OK


class GpuOpsError(Exception):
    def __init__(self, code: ExitCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
