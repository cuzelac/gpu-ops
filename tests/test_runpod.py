"""RunPod adapter against canned REST v2 responses (shapes taken from the live API on 2026-11-11)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from gpu_ops.errors import ExitCode, GpuOpsError
from gpu_ops.providers.base import CreateRequest, Offer
from gpu_ops.providers.runpod import RunPodProvider

CATALOG = {"gpus": [
    {"id": "NVIDIA GeForce RTX 5090", "name": "RTX 5090", "memory": 32, "price": {"community": 0.69, "secure": 1.19},
     "maxCount": {"community": 8, "secure": 8}},
    # placeholder community price on a tier with no machines: must never be offered
    {"id": "NVIDIA H200 NVL", "name": "H200 NVL", "memory": 143, "price": {"community": 0.5, "secure": 3.79},
     "maxCount": {"community": 0, "secure": 8}},
    {"id": "NVIDIA RTX A5000", "name": "RTX A5000", "memory": 24, "price": {"community": 0.16, "secure": 0.27},
     "maxCount": {"community": 10, "secure": 10}},
    {"id": "NVIDIA GeForce RTX 3070", "name": "RTX 3070", "memory": 8, "price": {"community": 0.13, "secure": 0},
     "maxCount": {"community": 6, "secure": 0}},
]}
# live stock per tier (include=AVAILABILITY&product=POD), as on 2026-11-12: the cheap A5000 is out of stock
STOCK = {"SECURE": {"NVIDIA GeForce RTX 5090": "LOW", "NVIDIA H200 NVL": "LOW", "NVIDIA RTX A5000": "NONE"},
         "COMMUNITY": {"NVIDIA GeForce RTX 5090": "HIGH", "NVIDIA H200 NVL": "LOW", "NVIDIA RTX A5000": "NONE",
                       "NVIDIA GeForce RTX 3070": "HIGH"}}


def catalog(req: httpx.Request) -> httpx.Response:
    assert req.url.params["include"] == "AVAILABILITY" and req.url.params["product"] == "POD"
    cloud = req.url.params["cloud"]
    gpus = [{**g, "availability": STOCK[cloud].get(g["id"], "NONE")} for g in CATALOG["gpus"]]
    return httpx.Response(200, json={"gpus": gpus})


POD = {"id": "abc123", "name": "gpuops-x", "status": "RUNNING", "cost": 0.69, "cloud": "COMMUNITY",
       "dataCenterId": "EU-RO-1", "createdAt": "2026-11-11T18:00:00Z",
       "gpu": {"id": "NVIDIA GeForce RTX 5090", "count": 1},
       "ssh": {"proxy": {"host": "ssh.runpod.io", "port": 22, "username": "abc-123"},
               "direct": {"host": "203.0.113.7", "port": 40122, "username": "root"}}}


def make(handler) -> tuple[RunPodProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)
    client = httpx.Client(base_url="https://api.runpod.io", transport=httpx.MockTransport(wrapped))
    return RunPodProvider("test-key", client=client), seen


def test_offers_use_live_stock_not_maxcount():
    p, seen = make(catalog)
    offers = p.offers(min_vram_gb=16, count=1)
    keys = {(o.gpu_id, o.tier, o.price_per_hr) for o in offers}
    assert ("NVIDIA H200 NVL", "community", 0.5) not in keys  # placeholder price on a maxCount-0 tier
    assert ("NVIDIA H200 NVL", "secure", 3.79) in keys
    assert not any(o.gpu_id == "NVIDIA RTX A5000" for o in offers)  # maxCount 10 but stock NONE
    assert not any(o.gpu_id == "NVIDIA GeForce RTX 3070" for o in offers)  # 8 GB < 16
    by = {(o.gpu_id, o.tier): o.available for o in offers}
    assert by[("NVIDIA GeForce RTX 5090", "community")] == 3 and by[("NVIDIA GeForce RTX 5090", "secure")] == 1
    assert {r.url.params["cloud"] for r in seen} == {"SECURE", "COMMUNITY"}


def test_offers_multi_gpu_price_and_ceiling():
    p, seen = make(catalog)
    offers = {(o.gpu_id, o.tier): o for o in p.offers(min_vram_gb=24, count=8)}
    assert offers[("NVIDIA GeForce RTX 5090", "community")].price_per_hr == 5.52
    assert all(r.url.params["count"] == "8" for r in seen)
    assert not p.offers(min_vram_gb=24, count=9)  # above every maxCount ceiling


def test_no_capacity_on_create_is_exit_4():
    p, _ = make(lambda r: httpx.Response(400, json={"detail": "There are no longer any instances available with the "
                                                              "requested specifications. Please refresh and try again."}))
    offer = Offer("runpod", "NVIDIA RTX A5000", "RTX A5000", 24, 1, "community", 0.16, 1)
    with pytest.raises(GpuOpsError) as e:
        p.create(CreateRequest(name="gpuops-x", offer=offer, image="i", bootstrap="", public_key=""))
    assert e.value.code == ExitCode.NO_OFFER


def test_create_sends_bootstrap_key_port_and_tier():
    p, seen = make(lambda r: httpx.Response(201, json={**POD, "status": "PROVISIONING", "ssh": None}))
    offer = Offer("runpod", "NVIDIA GeForce RTX 5090", "RTX 5090", 32, 1, "community", 0.69, 8)
    inst = p.create(CreateRequest(name="gpuops-x", offer=offer, image="img@sha256:" + "0" * 64,
                                  bootstrap="echo boot", public_key="ssh-ed25519 AAAA test"))
    body = json.loads(seen[0].content)
    assert body["cloud"] == "COMMUNITY" and body["ports"] == ["22/tcp"]
    assert body["entrypoint"] == ["bash", "-c", "echo boot"] and body["cmd"] == []
    assert body["env"]["PUBLIC_KEY"] == "ssh-ed25519 AAAA test"
    assert body["gpu"] == {"id": "NVIDIA GeForce RTX 5090", "count": 1}
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert inst.status == "creating" and inst.ssh is None


def test_get_maps_direct_ssh_only():
    p, _ = make(lambda r: httpx.Response(200, json=POD))
    inst = p.get("abc123")
    assert inst and inst.status == "running" and inst.ssh and inst.ssh.port == 40122
    assert inst.tier == "community" and inst.price_per_hr == 0.69
    p2, _ = make(lambda r: httpx.Response(200, json={**POD, "ssh": {"proxy": POD["ssh"]["proxy"], "direct": None}}))
    assert p2.get("abc123").ssh is None  # proxy-only is not good enough for rsync


def test_get_404_means_gone_and_terminate_is_idempotent():
    p, _ = make(lambda r: httpx.Response(404, json={"error": "not found"}))
    assert p.get("gone") is None
    p.terminate("gone")  # no exception


def test_list_filters_prefix_and_terminated_and_paginates():
    pages = iter([
        {"pods": [{**POD, "id": "1", "name": "gpuops-a"}, {**POD, "id": "2", "name": "mine"}],
         "pagination": {"hasNextPage": True, "nextCursor": "c1"}},
        {"pods": [{**POD, "id": "3", "name": "gpuops-b", "status": "TERMINATED"},
                  {**POD, "id": "4", "name": "gpuops-c", "status": "EXITED"}],
         "pagination": {"hasNextPage": False, "nextCursor": None}},
    ])
    p, seen = make(lambda r: httpx.Response(200, json=next(pages)))
    ids = [i.id for i in p.list_instances("gpuops-")]
    assert ids == ["1", "4"]  # EXITED still counts: it is ours and may bill for disk
    assert seen[1].url.params["cursor"] == "c1"


def test_permission_error_is_explained():
    p, _ = make(lambda r: httpx.Response(403, json={"error": "forbidden"}))
    with pytest.raises(GpuOpsError) as e:
        p.get("x")
    assert e.value.code == ExitCode.PROVIDER_ERROR and "permission" in e.value.message


def test_billed_cost():
    body = {"metadata": {"recordCount": 1, "totals": {"totalAmount": 0.0123}}, "records": [{}]}
    p, seen = make(lambda r: httpx.Response(200, json=body))
    assert p.billed_cost("abc123", datetime(2026, 11, 11, 18, tzinfo=timezone.utc)) == pytest.approx(0.0123)
    assert seen[0].url.params["podId"] == "abc123"
    p2, _ = make(lambda r: httpx.Response(200, json={"metadata": {"recordCount": 0, "totals": {}}, "records": []}))
    assert p2.billed_cost("abc123", datetime.now(timezone.utc)) is None
