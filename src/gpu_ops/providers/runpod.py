"""RunPod adapter, REST API v2 (https://api.runpod.io; v1 retires 15 Nov 2026).

Facts this adapter depends on (checked 2026-11-11 against /v2/openapi.json and live calls):
- GET /v2/catalog/gpus: per GPU {id, memory, price{secure,community}, maxCount{secure,community}}.
  A tier with maxCount 0 can carry a placeholder price (e.g. $0.50 for an H200 NVL); never offer it.
  maxCount is the largest machine, not stock. Stock: ?include=AVAILABILITY&product=POD&cloud=…&count=…
  adds `availability` NONE|LOW|MEDIUM|HIGH.
- POST /v2/pods: name, image, cmd/entrypoint (exec form), env, ports, disk, cloud (SECURE|COMMUNITY),
  gpu{id,count}. No tags field and no max-runtime field.
- GET /v2/pods/{id}: status PROVISIONING|STARTING|RUNNING|EXITED|ERROR|TERMINATED; ssh.direct{host,port}
  only when 22/tcp is published and assigned (rsync needs direct; the proxy is shell-only). cost = $/hr.
- DELETE /v2/pods/{id}: 204; 404 if already gone.
- GET /v2/billing/pods?podId=…: metadata.totals.totalAmount.
- Key scopes used: pods:read/write, catalog:read, billing:read.
- Self-termination inside a pod relies on RUNPOD_POD_ID and RUNPOD_API_KEY being injected.
  RunPod's docs do not promise the key; the dead-man check reports whether it is there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..errors import ExitCode, GpuOpsError
from ..remote import SshRemote
from .base import CreateRequest, Instance, Offer, SelfTerminate, SshEndpoint

BASE_URL = "https://api.runpod.io"

AVAILABILITY_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}  # Offer.available holds this rank

_STATUS = {"PROVISIONING": "creating", "STARTING": "creating", "RUNNING": "running", "EXITED": "exited",
           "ERROR": "error", "TERMINATED": "terminated"}


class RunPodProvider:
    name = "runpod"

    def __init__(self, api_key: str, *, client: httpx.Client | None = None, base_url: str = BASE_URL):
        self._client = client or httpx.Client(base_url=base_url, timeout=30)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    # --- http ---
    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._client.request(method, path, headers=self._headers, **kw)
        except httpx.HTTPError as e:
            raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"RunPod {method} {path}: {type(e).__name__}: {e}")
        if r.status_code in (401, 403):
            raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"RunPod {method} {path}: HTTP {r.status_code} — the API key "
                                                       f"lacks a permission this call needs. Body: {r.text[:300]}")
        return r

    @staticmethod
    def _check(r: httpx.Response, what: str) -> httpx.Response:
        if r.status_code >= 400:
            raise GpuOpsError(ExitCode.PROVIDER_ERROR, f"RunPod {what}: HTTP {r.status_code}: {r.text[:500]}")
        return r

    # --- Provider ---
    def offers(self, *, min_vram_gb: int, count: int) -> list[Offer]:
        """Only GPUs RunPod says are in stock now (include=AVAILABILITY&product=POD), one catalog call per tier.

        `maxCount` is NOT stock: RunPod's docs call it "a ceiling, not a stock level". A first smoke run on
        2026-11-12 was refused with "no longer any instances available" for a GPU whose maxCount was 10.
        """
        out: list[Offer] = []
        for tier in ("secure", "community"):
            params = {"include": "AVAILABILITY", "product": "POD", "count": count, "cloud": tier.upper()}
            data = self._check(self._req("GET", "/v2/catalog/gpus", params=params), "list GPU types").json()
            for g in data.get("gpus", []):
                mem = int(g.get("memory") or 0)
                price = (g.get("price") or {}).get(tier) or 0
                level = AVAILABILITY_RANK.get(g.get("availability") or "NONE", 0)
                ceiling = (g.get("maxCount") or {}).get(tier) or 0
                if mem < min_vram_gb or price <= 0 or level == 0 or ceiling < count:
                    continue
                out.append(Offer(provider=self.name, gpu_id=g["id"], gpu_name=g.get("name") or g["id"], vram_gb=mem,
                                 count=count, tier=tier, price_per_hr=round(price * count, 4), available=level))
        return out

    def create(self, req: CreateRequest) -> Instance:
        body = {
            "name": req.name,
            "image": req.image,
            "entrypoint": ["bash", "-c", req.bootstrap],
            "cmd": [],  # clear the image's CMD so nothing is appended to our bootstrap
            "env": {**req.env, "PUBLIC_KEY": req.public_key},
            "ports": ["22/tcp"],
            "disk": req.disk_gb,
            "cloud": req.offer.tier.upper(),
            "gpu": {"id": req.offer.gpu_id, "count": req.offer.count},
        }
        r = self._req("POST", "/v2/pods", json=body)
        text = r.text.lower()
        if r.status_code in (400, 409, 422) and ("available" in text or "capacity" in text):
            # seen 2026-11-12: HTTP 400 "There are no longer any instances available with the requested specifications"
            raise GpuOpsError(ExitCode.NO_OFFER, f"RunPod could not place the pod: HTTP {r.status_code}: {r.text[:300]}")
        return self._instance(self._check(r, "create pod").json())

    def get(self, instance_id: str) -> Instance | None:
        r = self._req("GET", f"/v2/pods/{instance_id}")
        if r.status_code == 404:
            return None
        return self._instance(self._check(r, "get pod").json())

    def list_instances(self, prefix: str) -> list[Instance]:
        out, cursor = [], None
        while True:
            params = {"limit": 1000, **({"cursor": cursor} if cursor else {})}
            data = self._check(self._req("GET", "/v2/pods", params=params), "list pods").json()
            out += [self._instance(p) for p in data.get("pods", []) if (p.get("name") or "").startswith(prefix)]
            pg = data.get("pagination") or {}
            if not pg.get("hasNextPage"):
                break
            cursor = pg.get("nextCursor")
        return [i for i in out if i.alive]

    def terminate(self, instance_id: str) -> None:
        r = self._req("DELETE", f"/v2/pods/{instance_id}")
        if r.status_code not in (200, 202, 204, 404):
            self._check(r, "terminate pod")

    def billed_cost(self, instance_id: str, since: datetime) -> float | None:
        params = {"podId": instance_id, "bucketSize": "day",
                  "startTime": since.astimezone(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")}
        r = self._req("GET", "/v2/billing/pods", params=params)
        if r.status_code >= 400:
            return None
        meta = (r.json().get("metadata") or {})
        if not meta.get("recordCount"):
            return None
        return float((meta.get("totals") or {}).get("totalAmount") or 0)

    def self_terminate(self) -> SelfTerminate | None:
        return SelfTerminate(
            check='[ -n "${RUNPOD_POD_ID:-}" ] && [ -n "${RUNPOD_API_KEY:-}" ] && command -v curl >/dev/null',
            kill=('curl -s -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" '
                  '"https://api.runpod.io/v2/pods/$RUNPOD_POD_ID" '
                  '|| runpodctl pod delete "$RUNPOD_POD_ID"'),
            diagnose=('for v in RUNPOD_POD_ID RUNPOD_API_KEY; do [ -n "${!v:-}" ] && echo "$v=set" || echo "$v=missing"; '
                      'done; command -v curl >/dev/null && echo curl=yes || echo curl=no; '
                      'command -v runpodctl >/dev/null && echo runpodctl=yes || echo runpodctl=no; '
                      'echo "provider_env_vars=$(cut -d= -f1 /gpuops/.provider_env 2>/dev/null | '
                      'sed "s/declare -x //" | tr "\\n" " ")"'))

    # --- mapping ---
    def _instance(self, p: dict) -> Instance:
        ssh = None
        direct = ((p.get("ssh") or {}).get("direct") or None)
        if direct and direct.get("host") and direct.get("port"):
            ssh = SshEndpoint(host=direct["host"], port=int(direct["port"]), user=direct.get("username") or "root")
        created = p.get("createdAt")
        gpu = p.get("gpu") or {}
        return Instance(
            provider=self.name, id=p["id"], name=p.get("name") or "", status=_STATUS.get(p.get("status"), "unknown"),
            created_at=datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
            price_per_hr=p.get("cost"), gpu_id=gpu.get("id"), gpu_count=gpu.get("count"),
            tier=(p.get("cloud") or "").lower() or None, data_center=p.get("dataCenterId"), ssh=ssh)

    def remote_for(self, inst: Instance, key_path: Path) -> SshRemote | None:
        if inst.ssh is None:
            return None
        return SshRemote(inst.ssh.host, inst.ssh.port, key_path, user=inst.ssh.user)
