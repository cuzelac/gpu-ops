"""Run ids and instance names.

Instance name: gpuops-<run-id>-d<deadline-epoch>-<job-name>
  e.g. gpuops-20261111T183000-a1b2-d1762890000-hs-qlora-v1b

The deadline is in the name so the reaper can act from the provider's own listing,
with no local state at all.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

PREFIX = "gpuops-"
_NAME = re.compile(r"^gpuops-(?P<run>\d{8}T\d{6}-[0-9a-f]{4})-d(?P<deadline>\d{9,11})(?:-(?P<job>[a-z0-9-]*))?$")


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"


def instance_name(run_id: str, deadline_epoch: int, job_name: str) -> str:
    return f"{PREFIX}{run_id}-d{deadline_epoch}-{job_name}"[:100]


@dataclass(frozen=True)
class ParsedName:
    run_id: str
    deadline_epoch: int
    job_name: str


def parse_instance_name(name: str) -> ParsedName | None:
    m = _NAME.match(name or "")
    if not m:
        return None
    return ParsedName(m["run"], int(m["deadline"]), m["job"] or "")
