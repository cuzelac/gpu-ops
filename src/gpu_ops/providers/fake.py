"""A provider that rents nothing. For tests and for trying gpu-ops without spending.

- Instances are directories under `root`; commands run with local bash (LocalRemote).
- State is kept in root/state.json, so a separate process (e.g. one killed mid-run in a test)
  leaves inspectable evidence.
- Failures can be injected: fail_create, terminate_failures (how many terminate calls fail),
  deadman_armable.

Enabled only when GPUOPS_ENABLE_FAKE=1 (configured from GPUOPS_FAKE_ROOT, default a temp dir).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..remote import LocalRemote
from ..names import PREFIX
from .base import CreateRequest, Instance, Offer, SelfTerminate

DEFAULT_OFFERS = [
    Offer("fake", "FAKE-24GB", "Fake 24 GB", 24, 1, "community", 0.20, 5),
    Offer("fake", "FAKE-24GB", "Fake 24 GB", 24, 1, "secure", 0.30, 5),
    Offer("fake", "FAKE-80GB", "Fake 80 GB", 80, 1, "community", 1.20, 2),
    Offer("fake", "FAKE-80GB-GONE", "Fake 80 GB (sold out)", 80, 1, "secure", 0.01, 0),
]


class FakeProvider:
    name = "fake"

    def __init__(self, root: Path | None = None, *, offers: list[Offer] | None = None, fail_create: bool = False,
                 terminate_failures: int = 0, deadman_armable: bool = True, no_capacity: set[tuple[str, str]] | None = None,
                 list_raises: str | None = None):
        self.no_capacity = no_capacity or set()  # {(gpu_id, tier)} whose create fails like RunPod's "no instances available"
        self.root = Path(root or os.environ.get("GPUOPS_FAKE_ROOT") or tempfile.mkdtemp(prefix="gpuops-fake-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self._offers = offers if offers is not None else DEFAULT_OFFERS
        self.fail_create = fail_create
        self.terminate_failures = terminate_failures
        self.deadman_armable = deadman_armable
        self.list_raises = list_raises  # message of a RuntimeError from list_instances, to exercise
        self.calls: list[str] = []      # the teardown-bookkeeping error path in Runner._release

    # --- persistent state ---
    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _load(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        return {"next": 1, "instances": {}}

    def _save(self, st: dict) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def _to_instance(self, d: dict) -> Instance:
        return Instance(provider="fake", id=d["id"], name=d["name"], status=d["status"],
                        created_at=datetime.fromisoformat(d["created_at"]), price_per_hr=d["price"],
                        gpu_id=d["gpu_id"], gpu_count=1, tier=d["tier"], data_center="FAKE-DC")

    # --- Provider ---
    def offers(self, *, min_vram_gb: int, count: int) -> list[Offer]:
        self.calls.append("offers")
        return [o for o in self._offers if o.available > 0 and o.vram_gb >= min_vram_gb and o.count == count]

    def create(self, req: CreateRequest) -> Instance:
        self.calls.append("create")
        if self.fail_create:
            raise RuntimeError("injected create failure")
        if (req.offer.gpu_id, req.offer.tier) in self.no_capacity:
            from ..errors import ExitCode, GpuOpsError
            raise GpuOpsError(ExitCode.NO_OFFER, f"injected: no capacity for {req.offer.gpu_id} {req.offer.tier}")
        st = self._load()
        iid = f"fake-{st['next']}"
        st["next"] += 1
        d = {"id": iid, "name": req.name, "status": "running", "created_at": datetime.now(timezone.utc).isoformat(),
             "price": req.offer.price_per_hr, "gpu_id": req.offer.gpu_id, "tier": req.offer.tier}
        st["instances"][iid] = d
        self._save(st)
        (self.root / iid).mkdir(exist_ok=True)
        return self._to_instance(d)

    def get(self, instance_id: str) -> Instance | None:
        d = self._load()["instances"].get(instance_id)
        return self._to_instance(d) if d else None

    def list_instances(self, prefix: str) -> list[Instance]:
        self.calls.append("list")
        if self.list_raises and prefix not in ("", PREFIX):
            # only the teardown lookup, which asks for one instance by full name; the planner
            # and `ps` ask for the bare prefix and must keep working
            raise RuntimeError(self.list_raises)
        return [self._to_instance(d) for d in self._load()["instances"].values()
                if d["status"] != "terminated" and d["name"].startswith(prefix)]

    def terminate(self, instance_id: str) -> None:
        self.calls.append("terminate")
        if self.terminate_failures > 0:
            self.terminate_failures -= 1
            raise RuntimeError("injected terminate failure")
        st = self._load()
        if instance_id in st["instances"]:
            st["instances"][instance_id]["status"] = "terminated"
            self._save(st)

    def billed_cost(self, instance_id: str, since: datetime) -> float | None:
        d = self._load()["instances"].get(instance_id)
        return None if d is None else 0.0

    def self_terminate(self) -> SelfTerminate | None:
        marker = self.root / "deadman-fired"
        return SelfTerminate(check="true" if self.deadman_armable else "false",
                             kill=f"touch {marker}", diagnose="echo FAKE_KEY=missing")

    # --- remote for this provider ---
    def remote_for(self, inst: Instance, key_path: Path) -> LocalRemote:
        return LocalRemote(self.root / inst.id)
