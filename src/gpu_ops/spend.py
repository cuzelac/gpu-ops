"""Spend and usage report from run records, for the ongoing rent-vs-buy decision.

The dict returned by `summarize` is a stable interface: the later HTML report reads it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .policy import Policy

SINGLE_CARD_VRAM_GB = 96  # an RTX PRO 6000-class card; jobs above this (or multi-GPU) a single card can't replace
GRACE_MIN = 10  # the reaper's own grace: past the deadline plus this, the machine is gone either way


def in_flight(r: dict, now: datetime | None = None) -> bool:
    """A detached run that is still going, as opposed to one whose process died.

    On disk the two look identical — an instance and no `ended_at` — so without this the
    report tells the human a healthy `run --detach` "died before teardown". Both are still
    costed at their upper bound; only the wording differs.
    """
    if r.get("status") != "running" or r.get("ended_at"):
        return False
    try:
        deadline = datetime.fromisoformat(r["deadline"])
    except (KeyError, TypeError, ValueError):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) < deadline + timedelta(minutes=GRACE_MIN)


def _unfinished(r: dict) -> bool:
    """A run whose gpu-ops process died before teardown (e.g. kill -9): an instance but no end.

    A detached run still in flight is in that state on purpose, so it is not counted here.
    """
    return (bool(r.get("instance_id")) and not r.get("ended_at")
            and r.get("cost_billed_usd") is None and not in_flight(r))


def _upper_bound(r: dict) -> tuple[float, float]:
    """(usd, seconds) for a run with no end: billed until its deadline, the latest the dead-man/reaper allow."""
    try:
        secs = max(0.0, (datetime.fromisoformat(r["deadline"]) - datetime.fromisoformat(r["started_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0, 0.0
    return (r.get("price_per_hr") or 0) * secs / 3600, secs


def _cost(r: dict) -> float:
    if r.get("cost_billed_usd") is not None:
        return r["cost_billed_usd"]
    # an in-flight detached run is priced at its worst case too, so the monthly cap keeps
    # reserving the money it may still spend
    if _unfinished(r) or in_flight(r):
        return _upper_bound(r)[0]
    return r.get("cost_estimate_usd") or 0.0


def _derive(r: dict) -> dict:
    """Everything `summarize` needs from one record, computed before anything is committed.

    Raises if a field is not the type this version expects — including the arithmetic, which
    is forced here rather than left to the caller's `+=`. The caller counts the record as
    unreadable and commits nothing, so no record can be half-counted.
    """
    secs = (_upper_bound(r)[1] if (_unfinished(r) or in_flight(r))
            else (r.get("billed_seconds") or 0))
    phases = r.get("phases") or {}
    return {
        "cost": _cost(r) + 0.0,
        "hours": secs / 3600 * (r.get("gpu_count") or 1) + 0.0,
        "unfinished": int(_unfinished(r)),
        "in_flight": int(in_flight(r)),
        "command_s": phases.get("command", 0) + 0.0,
        "overhead_s": sum(v for k, v in phases.items() if k != "command") + 0.0,
        "not_replaceable": int((r.get("vram_gb") or 0) > SINGLE_CARD_VRAM_GB or (r.get("gpu_count") or 1) > 1),
        "unreconciled": int(r.get("cost_billed_usd") is None and bool(r.get("instance_id"))),
        "by": (("month", r["started_at"][:7]), ("provider", r.get("provider")),
               ("project", r.get("project")), ("kind", r.get("kind")), ("gpu", r.get("gpu_name"))),
    }


def summarize(records: list[dict], policy: Policy | None, now: datetime | None = None) -> dict:
    """Totals over the run records. Tolerates a record it did not write.

    Every other reader of persisted state skips what it cannot parse rather than failing the
    command (`Ledger.current`, `Ledger.records`, the reconcile loop). This one is the reader
    that runs on *every* `gpu-ops spend`, so it does the same: a record whose fields are not
    the types this version expects is counted in `unreadable_records` and left out. A total
    that silently omits a run would be worse, so the count is reported, not swallowed.
    """
    now = now or datetime.now(timezone.utc)
    recs = [r for r in records if r.get("started_at")]
    unreadable = 0
    by = {k: defaultdict(lambda: {"usd": 0.0, "gpu_hours": 0.0, "runs": 0})
          for k in ("month", "provider", "project", "kind", "gpu")}
    overhead_s = command_s = 0.0
    total = gpu_hours_total = 0.0
    not_replaceable = 0
    unreconciled = 0
    unfinished = 0
    running = 0
    for r in recs:
        try:
            row = _derive(r)
        except (TypeError, ValueError, AttributeError, KeyError, IndexError):
            unreadable += 1  # a field that is not the type this version expects
            continue
        # from here nothing can raise, so a record is either counted everywhere or nowhere.
        # Tolerating a failure *mid-commit* is how `runs: 0` once shipped beside `total: 7.5`.
        unfinished += row["unfinished"]
        running += row["in_flight"]
        for key, val in row["by"]:
            b = by[key][val or "?"]
            b["usd"] += row["cost"]
            b["gpu_hours"] += row["hours"]
            b["runs"] += 1
        total += row["cost"]
        gpu_hours_total += row["hours"]
        command_s += row["command_s"]
        overhead_s += row["overhead_s"]
        not_replaceable += row["not_replaceable"]
        unreconciled += row["unreconciled"]

    # trailing 3 calendar months including this one
    months = []
    y, m = now.year, now.month
    for _ in range(3):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    trailing = sum(by["month"][mo]["usd"] for mo in months if mo in by["month"]) / 3

    rvb = policy.rent_vs_buy if policy else None
    card = rvb.card_price_usd if rvb else 0
    out = {
        "total_usd": round(total, 4),
        "gpu_hours": round(gpu_hours_total, 3),
        "runs": len(recs) - unreadable,
        "unreadable_records": unreadable,
        "unreconciled_runs": unreconciled,
        "unfinished_runs_counted_at_upper_bound": unfinished,
        # detached runs still going: also costed at their worst case, but they have not died
        "runs_in_flight": running,
        "trailing_3mo_monthly_usd": round(trailing, 4),
        "overhead_share": round(overhead_s / (overhead_s + command_s), 3) if (overhead_s + command_s) else None,
        "runs_a_single_card_could_not_replace": not_replaceable,
        "rent_vs_buy": {
            "card_price_usd": card or None,
            "spent_vs_card": round(total / card, 3) if card else None,
            "months_to_break_even_at_trailing_rate":
                round((card - total) / trailing, 1) if (card and trailing > 0 and card > total) else None,
            "note": None if card else "set rent_vs_buy.card_price_usd in policy.toml to track break-even",
        },
        "warning": None,
        "by": {k: {kk: {"usd": round(vv["usd"], 4), "gpu_hours": round(vv["gpu_hours"], 3), "runs": vv["runs"]}
                   for kk, vv in sorted(v.items())} for k, v in by.items()},
    }
    if rvb and rvb.warn_monthly_usd and trailing > rvb.warn_monthly_usd:
        out["warning"] = (f"trailing 3-month average ${trailing:.2f}/month is above your warning point "
                          f"${rvb.warn_monthly_usd:.2f}: time to revisit rent vs buy")
    return out
