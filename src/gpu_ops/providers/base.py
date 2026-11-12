"""The provider interface every adapter implements, and the shared data shapes.

Adding a provider = one new module in this package implementing `Provider`,
plus one line in providers/__init__.py. Nothing in the core changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Protocol

Tier = Literal["secure", "community"]
Status = Literal["creating", "running", "exited", "error", "terminated", "unknown"]


@dataclass(frozen=True)
class Offer:
    provider: str
    gpu_id: str  # provider's id, passed back to create()
    gpu_name: str  # human-readable
    vram_gb: int  # per GPU
    count: int
    tier: Tier
    price_per_hr: float  # total for `count` GPUs, USD
    available: int  # how many the provider says it can give (0 = do not offer)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SshEndpoint:
    host: str
    port: int
    user: str = "root"


@dataclass
class Instance:
    provider: str
    id: str
    name: str
    status: Status
    created_at: datetime | None = None
    price_per_hr: float | None = None
    gpu_id: str | None = None
    gpu_count: int | None = None
    tier: str | None = None
    data_center: str | None = None
    ssh: SshEndpoint | None = None  # set only when direct SSH (rsync-capable) is available

    @property
    def alive(self) -> bool:
        return self.status not in ("terminated",)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat() if self.created_at else None
        return d


@dataclass
class CreateRequest:
    name: str
    offer: Offer
    image: str
    bootstrap: str  # shell script the container runs as its main process
    public_key: str  # per-run SSH public key
    disk_gb: int = 50
    env: dict[str, str] = field(default_factory=dict)  # non-secret only; providers may store/display it


@dataclass
class SelfTerminate:
    """Shell snippets for the on-instance dead-man timer."""
    check: str  # exits 0 if the instance can delete itself
    kill: str  # deletes the instance
    diagnose: str = ""  # prints what is missing when check fails: names and yes/no only, NEVER values


class Provider(Protocol):
    name: str

    def offers(self, *, min_vram_gb: int, count: int) -> list[Offer]:
        """Rentable offers with available > 0 and a real price. Filtering by tier/allow/price is the planner's job."""

    def create(self, req: CreateRequest) -> Instance: ...

    def get(self, instance_id: str) -> Instance | None:
        """None means the provider no longer knows the instance (deleted)."""

    def list_instances(self, prefix: str) -> list[Instance]:
        """All live instances whose name starts with prefix. Must ask the provider, never a local cache."""

    def terminate(self, instance_id: str) -> None:
        """Idempotent: terminating an already-deleted instance is not an error."""

    def billed_cost(self, instance_id: str, since: datetime) -> float | None:
        """Provider-reported cost so far, or None if the provider can't say (yet)."""

    def self_terminate(self) -> SelfTerminate | None:
        """How an instance deletes itself, or None if this provider has no way."""
