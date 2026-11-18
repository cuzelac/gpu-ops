"""ps / kill / reap: act on what the PROVIDERS say is running, never only on local state.

Only instances whose name starts with `gpuops-` are ever touched.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .names import PREFIX, parse_instance_name
from .providers.base import Instance, Provider
from .runner import teardown


def ps(providers: dict[str, Provider], ledger_state: dict[str, dict] | None = None) -> list[dict]:
    now = datetime.now(timezone.utc)
    by_instance = {v.get("instance_id"): v for v in (ledger_state or {}).values() if v.get("instance_id")}
    rows = []
    for pname, p in providers.items():
        for inst in p.list_instances(PREFIX):
            rows.append(_row(inst, now, by_instance.get(inst.id)))
    return rows


def _row(inst: Instance, now: datetime, led: dict | None) -> dict:
    parsed = parse_instance_name(inst.name)
    age_min = (now - inst.created_at).total_seconds() / 60 if inst.created_at else None
    deadline = datetime.fromtimestamp(parsed.deadline_epoch, timezone.utc) if parsed else None
    return {
        **inst.to_dict(),
        "run_id": parsed.run_id if parsed else None,
        "job": parsed.job_name if parsed else None,
        "deadline": deadline.isoformat() if deadline else None,
        "minutes_to_deadline": round((deadline - now).total_seconds() / 60, 1) if deadline else None,
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "cost_so_far_usd": round(inst.price_per_hr * age_min / 60, 4) if (inst.price_per_hr and age_min) else None,
        "project": (led or {}).get("project"),
    }


def kill(providers: dict[str, Provider], instance_ids: list[str] | None, *, all_: bool,
         timeout_s: float = 300) -> list[dict]:
    results = []
    for pname, p in providers.items():
        try:
            live = p.list_instances(PREFIX)
        except Exception as e:
            # a provider we cannot query may be holding live machines, and the caller must
            # still learn what the other providers did: report it as a failure, never raise
            # past the results we already have
            results.append({"provider": pname, "id": None, "name": None, "terminated": False,
                            "note": f"could not list instances: {type(e).__name__}: {e}"})
            continue
        for inst in live:
            if all_ or (instance_ids and inst.id in instance_ids):
                ok = teardown(p, inst.id, timeout_s)
                results.append({"provider": pname, "id": inst.id, "name": inst.name, "terminated": ok})
    if instance_ids and not all_:
        found = {r["id"] for r in results}
        for iid in instance_ids:
            if iid not in found:
                results.append({"id": iid, "terminated": None,
                                "note": "not found among live gpuops-* instances (already gone, or not gpu-ops')"})
    return results


def reap_decisions(instances: list[Instance], now: datetime, *, grace_min: float, max_age_min: float) -> list[tuple[Instance, str]]:
    """Which instances to delete, and why. Pure function, unit-tested."""
    out = []
    for inst in instances:
        if not inst.name.startswith(PREFIX):
            continue
        parsed = parse_instance_name(inst.name)
        if parsed:
            over = (now.timestamp() - parsed.deadline_epoch) / 60
            if over > grace_min:
                out.append((inst, f"{over:.0f} min past its deadline"))
        elif inst.created_at is not None:
            age = (now - inst.created_at).total_seconds() / 60
            if age > max_age_min:
                out.append((inst, f"name has no deadline and it is {age:.0f} min old (> {max_age_min:.0f})"))
    return out


def reap(providers: dict[str, Provider], *, grace_min: float, max_age_min: float, dry_run: bool,
         timeout_s: float = 300) -> list[dict]:
    now = datetime.now(timezone.utc)
    results = []
    for pname, p in providers.items():
        try:
            live = p.list_instances(PREFIX)
        except Exception as e:  # as in kill(): an unqueryable provider is a result, not a raise
            results.append({"provider": pname, "id": None, "name": None, "reason": "provider unreachable",
                            "terminated": False, "dry_run": dry_run,
                            "note": f"could not list instances: {type(e).__name__}: {e}"})
            continue
        for inst, why in reap_decisions(live, now, grace_min=grace_min, max_age_min=max_age_min):
            ok = None if dry_run else teardown(p, inst.id, timeout_s)
            # the run id is read out of the instance's own name, never the ledger: the
            # reaper has to keep working with no local state at all (docs/design.md §4)
            parsed = parse_instance_name(inst.name)
            results.append({"provider": pname, "id": inst.id, "name": inst.name, "reason": why,
                            "run_id": parsed.run_id if parsed else None,
                            "terminated": ok, "dry_run": dry_run})
    return results
