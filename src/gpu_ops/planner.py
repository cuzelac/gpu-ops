"""Choosing an offer and checking the run against every cost and time limit, before anything is created."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .errors import ExitCode, GpuOpsError
from .names import PREFIX
from .policy import Policy
from .providers.base import Offer, Provider
from .spec import GpuSpec, JobSpec

PREFER_WITHIN = 1.10  # a preferred GPU wins if it costs at most 10% more than the cheapest


@dataclass
class Plan:
    offer: Offer
    run_minutes: float  # the deadline the run gets: min(spec time cap, what the cost cap buys)
    worst_case_usd: float  # price × run_minutes
    month_spent_usd: float
    alternatives: list[Offer] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"offer": self.offer.to_dict(), "run_minutes": round(self.run_minutes, 2),
                "worst_case_usd": round(self.worst_case_usd, 4), "month_spent_usd": round(self.month_spent_usd, 4),
                "alternatives": [o.to_dict() for o in self.alternatives[:5]], "notes": self.notes}


def filter_offers(offers: list[Offer], gpu: GpuSpec) -> list[Offer]:
    out = []
    for o in offers:
        if o.available <= 0 or o.price_per_hr <= 0:
            continue
        if o.vram_gb < gpu.min_vram_gb or o.count != gpu.count:
            continue
        if gpu.tier != "any" and o.tier != gpu.tier:
            continue
        if gpu.allow and o.gpu_id not in gpu.allow:
            continue
        if gpu.max_price_per_hr and o.price_per_hr > gpu.max_price_per_hr:
            continue
        out.append(o)
    return sorted(out, key=lambda o: (o.price_per_hr, -o.vram_gb, o.gpu_id))


def choose(candidates: list[Offer], gpu: GpuSpec, notes: list[str]) -> Offer:
    if gpu.select != "cheapest-hour":
        notes.append(f"select={gpu.select} needs per-GPU timing records, which don't exist yet; "
                     "fell back to cheapest-hour")
    cheapest = candidates[0]
    for pref in gpu.prefer:
        for o in candidates:
            if o.gpu_id == pref and o.price_per_hr <= cheapest.price_per_hr * PREFER_WITHIN:
                notes.append(f"preferred {pref} chosen (within 10% of the cheapest)")
                return o
    return cheapest


def month_spend(records: list[dict], now: datetime) -> float:
    total = 0.0
    for r in records:
        started = r.get("started_at")
        if not started:
            continue
        t = datetime.fromisoformat(started)
        if (t.year, t.month) == (now.year, now.month):
            # same rule as `gpu-ops spend`: billed > estimate > upper bound for runs killed before teardown
            from .spend import _cost
            total += _cost(r)
    return total


def make_plan(spec: JobSpec, policy: Policy, providers: dict[str, Provider], records: list[dict],
              *, now: datetime | None = None) -> Plan:
    now = now or datetime.now(timezone.utc)
    lim, pl = spec.limits, policy.limits
    notes: list[str] = []

    names = [n for n in providers if n in policy.providers.allowed]
    if spec.gpu.providers:
        names = [n for n in names if n in spec.gpu.providers]
    if not names:
        raise GpuOpsError(ExitCode.REFUSED, f"no allowed provider: policy allows {policy.providers.allowed}, "
                                            f"spec asks for {spec.gpu.providers or 'any'}, configured {list(providers)}")
    if lim.max_minutes > pl.max_job_minutes:
        raise GpuOpsError(ExitCode.REFUSED, f"max_minutes {lim.max_minutes} exceeds policy max_job_minutes "
                                            f"{pl.max_job_minutes}")

    # concurrency: count what the providers say is alive, not what we remember
    alive = sum(len(providers[n].list_instances(PREFIX)) for n in names)
    if alive >= pl.max_concurrent:
        raise GpuOpsError(ExitCode.REFUSED, f"{alive} gpu-ops instance(s) already running; policy max_concurrent "
                                            f"is {pl.max_concurrent}. See `gpu-ops ps`.")

    offers: list[Offer] = []
    for n in names:
        offers += providers[n].offers(min_vram_gb=spec.gpu.min_vram_gb, count=spec.gpu.count)
    candidates = filter_offers(offers, spec.gpu)
    if not candidates:
        raise GpuOpsError(ExitCode.NO_OFFER, f"no available offer with ≥{spec.gpu.min_vram_gb} GB × {spec.gpu.count}, "
                                             f"tier={spec.gpu.tier}, allow={spec.gpu.allow or 'any'}, "
                                             f"max_price_per_hr={spec.gpu.max_price_per_hr or 'none'}")
    offer = choose(candidates, spec.gpu, notes)
    plan = plan_for_offer(spec, policy, offer, month_spend(records, now), notes)
    plan.alternatives = [o for o in candidates if o != offer]
    return plan


def plan_for_offer(spec: JobSpec, policy: Policy, offer: Offer, month_spent: float,
                   notes: list[str] | None = None) -> Plan:
    """Deadline and worst case for one offer, checked against the spec and policy. Raises REFUSED."""
    lim, pl = spec.limits, policy.limits
    notes = list(notes or [])
    # the run gets whichever is shorter: the time cap, or the minutes the cost cap buys at this price
    cost_minutes = lim.max_cost_usd / offer.price_per_hr * 60
    run_minutes = min(lim.max_minutes, cost_minutes)
    if cost_minutes < lim.max_minutes:
        notes.append(f"cost cap ${lim.max_cost_usd} buys {cost_minutes:.1f} min at ${offer.price_per_hr}/hr; "
                     f"deadline shortened from {lim.max_minutes} min")
    worst = offer.price_per_hr * run_minutes / 60
    if worst > pl.max_job_usd:
        raise GpuOpsError(ExitCode.REFUSED, f"worst case ${worst:.2f} exceeds policy max_job_usd ${pl.max_job_usd}")
    if month_spent + worst > pl.max_month_usd:
        raise GpuOpsError(ExitCode.REFUSED, f"this month ${month_spent:.2f} + worst case ${worst:.2f} exceeds policy "
                                            f"max_month_usd ${pl.max_month_usd}")
    return Plan(offer=offer, run_minutes=run_minutes, worst_case_usd=worst, month_spent_usd=month_spent, notes=notes)
