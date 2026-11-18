"""gpu-ops command line. Every command accepts --json and --get; nothing ever prompts.

stdout: the result (one JSON object with --json). `run` prints a compact summary by default
and the whole record with --full.
stderr: quiet by default. Phase lines go there when stderr is a terminal, or with --progress;
the job's own output only with --progress. `runs/<id>/job.log` is always written either way —
read it back with `gpu-ops logs`.
Exit codes: see errors.ExitCode and AGENTS.md.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from datetime import datetime
from typing import Any, Callable

from . import __version__
from .errors import ExitCode, GpuOpsError
from .ledger import Ledger
from .names import is_run_id
from .policy import Policy, init_config, load_policy, policy_path, providers_path
from .providers import load_providers
from .result import DEFAULT_LOG_TAIL, compact, dumps, read_log, result, select
from .secrets import Redactor, read_op


def _emit(args: argparse.Namespace, result: dict, human: Callable[[dict], str]) -> None:
    paths = getattr(args, "get", None)
    if paths:  # --get prints JSON whether or not --json was given
        print(dumps(select(result, paths)))
    elif args.json:
        print(dumps(result))
    else:
        print(human(result))


def _finish(args: argparse.Namespace, work: Callable[[], dict], human: Callable[[dict], str],
            *, fallback: int = int(ExitCode.INTERNAL)) -> int:
    """Run the rest of the command inside one guard, and return the code its result carries.

    `work` decides the exit code and builds the result together — the code is read back out
    of the result, so the two cannot disagree. Passing a callback rather than a finished
    dict is what makes the guard's scope a property of this function instead of a choice
    each command makes: a command cannot leave the statement that computes its own exit code
    outside the guard, which is what `kill`, `reap` and `spend` all used to do *after* they
    had already terminated instances or written to disk.

    Three failure modes, deliberately different:

    - `work` itself raises: there is no result and no code, so `fallback` is reported. For a
      command that has already acted that is the conservative code (exit 10 for `kill` and
      `reap`: gpu-ops cannot confirm the machines are gone), not a cheerful 0.
    - printing raises a `GpuOpsError` — a bad `--get` path, i.e. the caller's own mistake —
      and the command succeeded: exit 2, as AGENTS.md documents. The caller can retype it.
    - printing raises anything else, or the command had already failed: the command's own
      code stands and the whole result is printed raw. A formatting bug of ours must never
      replace the exit 10 that AGENTS.md rule 4 exists for, nor the exit 0 that says a
      machine was torn down.
    """
    try:
        res = work()
        code = int(res["exit_code"])
    except GpuOpsError:
        raise  # a deliberate outcome carrying its own exit code; main reports it as that
    except Exception as e:
        _err(f"error: could not produce the result ({type(e).__name__}: {e}); "
             f"reporting exit code {fallback}")
        print(dumps(result(fallback, error=f"result could not be produced: {type(e).__name__}: {e}")))
        return fallback
    try:
        _emit(args, res, human)
    except GpuOpsError as e:
        if code == int(ExitCode.OK):
            raise  # the caller mistyped a field path and nothing happened that outranks it
        _err(f"error: {e.message}; exit code {code} stands")
        print(dumps(res))  # the whole result: on exit 10 it says which machines are alive
    except Exception as e:
        _err(f"error: could not print the result ({type(e).__name__}: {e}); "
             f"exit code {code} stands")
        print(dumps(res))
    return code


def _noop(_line: str) -> None:
    pass


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _install_signal_handlers() -> None:
    """First SIGTERM/SIGHUP/SIGINT raises SystemExit so `finally` blocks tear down; later ones are ignored,
    so a second Ctrl-C cannot interrupt the teardown itself."""
    def handler(signum, frame):
        for s in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)):
            signal.signal(s, signal.SIG_IGN)
        _err(f"\nreceived signal {signum}: tearing down before exit (further signals ignored)")
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
        if s is not None:
            signal.signal(s, handler)


def _maybe_policy() -> Policy | None:
    try:
        return load_policy()
    except GpuOpsError:
        return None


def _all_providers(redactor: Redactor) -> dict:
    pol = _maybe_policy()
    names = pol.providers.allowed if pol else ["runpod"]
    if os.environ.get("GPUOPS_ENABLE_FAKE") == "1" and "fake" not in names:
        names = [*names, "fake"]
    return load_providers(names, redactor)


# ---------------- commands ----------------

def cmd_init(args) -> int:
    def work() -> dict:
        written = init_config(force=args.force)
        return result(0, written=[str(p) for p in written],
                      policy=str(policy_path()), providers=str(providers_path()))

    return _finish(args, work, lambda r: ("wrote " + ", ".join(r["written"])) if r["written"]
                   else f"already present: {r['policy']}, {r['providers']} (use --force to overwrite)")


def cmd_schema(args) -> int:
    from .spec import json_schema
    # wrapped, so `schema` carries the ok/exit_code every other command does; the schema
    # itself is under "schema" (AGENTS.md documents the shape, and --get drills into it)
    return _finish(args, lambda: result(0, schema=json_schema()), lambda r: dumps(r["schema"]))


def _spec_path(raw: str):
    """Accept C:/… paths from Windows-side callers as well as Linux paths."""
    from pathlib import Path
    from .paths import to_local_path
    return to_local_path(raw, Path.cwd())


def cmd_validate(args) -> int:
    from .spec import load_spec
    def work() -> dict:
        loaded = load_spec(_spec_path(args.spec))
        return result(0, spec=str(loaded.path), name=loaded.spec.name,
                      inputs={k: str(v) for k, v in loaded.input_paths.items()})

    return _finish(args, work, lambda r: f"OK: {r['spec']} ({r['name']})")


def cmd_pin_image(args) -> int:
    from .pin_image import pin
    return _finish(args, lambda: result(0, ref=args.ref, pinned=pin(args.ref)), lambda r: r["pinned"])


def cmd_run(args) -> int:
    from .planner import make_plan
    from .runner import Runner
    from .spec import load_spec

    loaded = load_spec(_spec_path(args.spec))
    policy = load_policy()
    redactor = Redactor()
    names = [n for n in policy.providers.allowed if not loaded.spec.gpu.providers or n in loaded.spec.gpu.providers]
    providers = load_providers(names, redactor)
    ledger = Ledger()
    plan = make_plan(loaded.spec, policy, providers, ledger.records())

    if args.dry_run:
        return _finish(args, lambda: result(0, dry_run=True, job=loaded.spec.name,
                                           plan=plan.to_dict(), would_create="nothing (dry run)"),
                       lambda r: (
            f"DRY RUN — nothing created.\n"
            f"  offer: {plan.offer.gpu_name} ×{plan.offer.count} ({plan.offer.tier}) on {plan.offer.provider} "
            f"at ${plan.offer.price_per_hr}/hr\n"
            f"  deadline: {plan.run_minutes:.1f} min   worst case: ${plan.worst_case_usd:.2f}   "
            f"spent this month: ${plan.month_spent_usd:.2f}\n" + "".join(f"  note: {n}\n" for n in plan.notes)))

    secrets = {k: read_op(ref, redactor) for k, ref in loaded.spec.secrets.items()}
    provider = providers[plan.offer.provider]
    progress = bool(getattr(args, "progress", False))
    detach = bool(getattr(args, "detach", False))
    # phase lines for a human watching a terminal, or on request; the job's own output only on request
    log = _err if (progress or sys.stderr.isatty()) else _noop
    # None, not a no-op: the runner only pays to fetch the job's log when someone reads it
    job_log = (lambda line: _err("  | " + line)) if progress else None
    _install_signal_handlers()
    runner = Runner(loaded, plan, policy, provider, ledger, provider.remote_for, redactor, secrets,
                    log=log, job_log=job_log)
    code, rec = runner.run(detach=detach)
    shown: dict = {}  # filled by work(), which runs inside _finish's guard

    def work() -> dict:
        run_dir = ledger.run_dir(rec.run_id)
        record = rec.to_dict()  # already redacted field by field, by the ledger
        brief = result(code, **compact(record, run_dir, [o.path for o in loaded.spec.outputs]))
        if detach and code == ExitCode.OK:
            brief.update({
                "detached": True,
                "instance_id": rec.instance_id,
                "deadline": rec.deadline,
                "collect_by": runner.job_deadline().isoformat(),
                "collect_window_min": round(runner.collect_window_s / 60, 1),
                "next": f"gpu-ops status {rec.run_id}",
            })
        shown["brief"] = brief
        shown["run_dir"] = run_dir
        if getattr(args, "full", False):
            return result(code, run_id=rec.run_id, run_dir=str(run_dir), record=record)
        return brief

    # the run is over and its code is known, so that is the honest fallback
    return _finish(args, work,
                   lambda _r: (_human_detached(shown["brief"]) if shown["brief"].get("detached")
                               else _human_run(shown["brief"], shown["run_dir"])),
                   fallback=int(code))


def _human_detached(b: dict) -> str:
    return "\n".join([
        f"DETACHED run {b['run_id']} is running on {b['instance_id']}",
        f"  {b['gpu']} ({b['tier']}) at ${b['price_per_hr']}/hr   dead-man: {b['deadman']}",
        f"  collect by {b['collect_by']} ({b['collect_window_min']} min before the deadline);"
        f" anything not collected by then is lost",
        f"  check:   gpu-ops status {b['run_id']}",
        f"  finish:  gpu-ops collect {b['run_id']}   (or `wait` to block until it is done)",
    ] + [f"  note: {n}" for n in b["notes"]])


def _human_run(b: dict, run_dir) -> str:
    lines = [f"{'OK' if b['ok'] else 'FAILED'} (exit {b['exit_code']}) run {b['run_id']}",
             f"  {b['gpu']} ({b['tier']}) at ${b['price_per_hr']}/hr   {b['minutes']} min   "
             f"est. ${b['cost_estimate_usd']}",
             f"  teardown confirmed: {b['teardown_confirmed']}   outputs verified: {b['outputs_verified']}"
             f"   dead-man: {b['deadman']}"]
    if b["outputs"]:
        lines.append(f"  outputs in {b['outputs_dir']}: " + ", ".join(
            f"{k} ({v['files']} files, {v['bytes']} B)" if isinstance(v, dict) else f"{k} ({v} B)"
            for k, v in b["outputs"].items()))
    if b.get("report"):
        lines.append("  report: " + ", ".join(b["report"]))
    lines += [f"  note: {n}" for n in b["notes"]]
    if b["error"]:
        lines.append(f"  error: {b['error']}")
    lines += [f"  | {x}" for x in (b.get("log_tail") or [])]
    lines.append(f"  record: {run_dir}/record.json")
    return "\n".join(lines)


def _detached(args):
    """Reattach to a detached run, or say why it cannot be reached."""
    from .detached import Detached
    redactor = Redactor()
    ledger = Ledger(redactor=redactor)
    log = _err if sys.stderr.isatty() or getattr(args, "progress", False) else _noop
    return Detached.load(ledger, args.run_id, _all_providers(redactor), log=log)


def cmd_status(args) -> int:
    """What a detached run is doing. Free, and changes nothing.

    Exits 0 whenever it could report — like `ps`, this is an observation. What the *job* did
    is in `phase` and `job_exit_code`; `collect` is what turns that into the run's own code.
    """
    d = _detached(args)
    return _finish(args, lambda: result(ExitCode.OK, **d.status()), lambda r: "\n".join(
        [f"run {r['run_id']}: {r['phase']} (status {r['status']})",
         f"  elapsed {r['elapsed_min']} min, deadline in {r['minutes_to_deadline']} min, "
         f"~${r['cost_so_far_usd']}"]
        + ([f"  collect by {r['collect_by']}"] if not r["collected"] else [])
        + ([f"  job exit code {r['job_exit_code']}"] if r["job_exit_code"] is not None else [])
        + ([f"  | {r['last_log_line']}"] if r["last_log_line"] else [])
        + ([f"  error: {r['error']}"] if r["error"] else [])))


def _collect_result(d, code: int, raw: dict) -> dict:
    run_dir = d.run_dir
    declared = [o.path for o in d.plan.outputs]
    return result(code, **compact(raw, run_dir, declared))


def cmd_collect(args) -> int:
    """Bring a detached run's outputs home and shut the machine down. Idempotent.

    Exit 11 means the job is still going and nothing was touched, so
    `until gpu-ops collect ID; do sleep 60; done` is a correct poll loop.
    """
    d = _detached(args)

    def work() -> dict:
        code, raw = d.collect()
        return _collect_result(d, int(code), raw)

    # it may already have terminated the instance by the time anything goes wrong here
    return _finish(args, work, lambda r: _human_run(r, d.run_dir),
                   fallback=int(ExitCode.TEARDOWN_FAILED))


def cmd_wait(args) -> int:
    """Block until a detached run ends, then collect. Streams nothing; prints one object."""
    d = _detached(args)

    def work() -> dict:
        code, raw = d.wait(args.timeout, poll_s=args.poll)
        return _collect_result(d, int(code), raw)

    return _finish(args, work, lambda r: _human_run(r, d.run_dir),
                   fallback=int(ExitCode.TEARDOWN_FAILED))


def cmd_logs(args) -> int:
    if not is_run_id(args.run_id):  # never let a caller-supplied id become a path
        raise GpuOpsError(ExitCode.SPEC_INVALID,
                          f"{args.run_id!r} is not a run id (expected e.g. 20261112T140915-7a06)")
    path = Ledger().run_dir(args.run_id) / "job.log"
    source = "local"
    if not path.is_file():
        # a detached run writes its log on the instance; until it is collected there is
        # nothing local to read, so fetch it rather than claiming the run has no log
        try:
            if _detached(args).fetch_log():
                source = "instance"
        except GpuOpsError:
            pass  # not a detached run, or the machine is unreachable: fall through to the error
    if not path.is_file():
        raise GpuOpsError(ExitCode.SPEC_INVALID, f"no job log for run {args.run_id!r} ({path} does not exist)")

    def work() -> dict:
        lines, in_file, matched = read_log(path, tail=args.tail, grep=args.grep, phase=args.phase)
        return result(0, run_id=args.run_id, path=str(path), source=source,
                      lines_in_file=in_file, lines_matched=matched, lines=lines)

    return _finish(args, work, lambda r: "\n".join(r["lines"]))


def cmd_ps(args) -> int:
    from .ops import ps
    return _finish(args, lambda: result(0, instances=ps(_all_providers(Redactor()), Ledger().current())),
                   lambda r: "\n".join(
        [f"{len(r['instances'])} gpu-ops instance(s) running"] +
        [f"  {x['provider']} {x['id']} {x['status']} {x['name']}  age {x['age_minutes']} min, "
         f"deadline in {x['minutes_to_deadline']} min, ~${x['cost_so_far_usd']}" for x in r["instances"]]))


def cmd_kill(args) -> int:
    from .ops import kill
    if not args.all and not args.ids:
        raise GpuOpsError(ExitCode.SPEC_INVALID, "give instance ids, or --all")
    def work() -> dict:
        res = kill(_all_providers(Redactor()), args.ids, all_=args.all)
        failed = [r for r in res if r.get("terminated") is False]
        return result(ExitCode.TEARDOWN_FAILED if failed else ExitCode.OK, results=res)

    # if deciding that fails we cannot claim the machines are gone
    return _finish(args, work, lambda r: "\n".join(
        [f"{len(r['results'])} instance(s) processed"]
        + [f"  {x.get('id')}: terminated={x.get('terminated')}"
           f"{'  ' + x['note'] if x.get('note') else ''}" for x in r["results"]]),
        fallback=int(ExitCode.TEARDOWN_FAILED))


def cmd_reap(args) -> int:
    from .ops import reap
    def work() -> dict:
        pol = _maybe_policy()
        max_age = args.max_age_min or ((pol.limits.max_job_minutes if pol else 360) + args.grace_min)
        res = reap(_all_providers(Redactor()), grace_min=args.grace_min, max_age_min=max_age,
                   dry_run=args.dry_run)
        failed = [r for r in res if r.get("terminated") is False]
        return result(ExitCode.TEARDOWN_FAILED if failed else ExitCode.OK, reaped=res)

    return _finish(args, work, lambda r: "\n".join(
        [f"{len(r['reaped'])} instance(s) {'would be ' if args.dry_run else ''}reaped"]
        + [f"  {x['id']} {x['name']}: {x['reason']} -> terminated={x['terminated']}"
           for x in r["reaped"]]),
        fallback=int(ExitCode.TEARDOWN_FAILED))


def cmd_spend(args) -> int:
    from .spend import summarize
    ledger = Ledger()
    reconciled = 0
    skipped: list[str] = []
    if args.reconcile:
        providers = _all_providers(Redactor())
        for path, r in ledger.records_with_paths():
            try:
                p = providers.get(r.get("provider") or "")
                if p is None or not r.get("instance_id") or r.get("cost_billed_usd") is not None:
                    continue
                billed = p.billed_cost(r["instance_id"], datetime.fromisoformat(r["started_at"]))
                if billed is not None:
                    # the file it came from, not the id inside it: those can disagree
                    ledger.patch_record(path, cost_billed_usd=round(billed, 4))
                    reconciled += 1
            except Exception as e:
                # a record this version cannot read, or a provider that refused: note it and
                # carry on. One unreadable file must not cost the caller the whole report.
                skipped.append(f"{r.get('run_id') or path.parent.name}: {type(e).__name__}: {e}")

    def work() -> dict:
        summary: dict[str, Any] = summarize(ledger.records(), _maybe_policy())
        summary["reconciled_now"] = reconciled
        summary["reconcile_skipped"] = skipped
        return result(0, **summary)

    def human(s: dict) -> str:
        lines = [f"{s['runs']} runs, ${s['total_usd']:.2f} total, {s['gpu_hours']} GPU-hours",
                 f"trailing 3-month average: ${s['trailing_3mo_monthly_usd']:.2f}/month",
                 f"overhead share of paid time: {s['overhead_share']}",
                 f"runs a single 96 GB card could not have run: {s['runs_a_single_card_could_not_replace']}"]
        rvb = s["rent_vs_buy"]
        lines.append(rvb["note"] or f"spent {rvb['spent_vs_card']:.1%} of the card price; "
                                    f"break-even in {rvb['months_to_break_even_at_trailing_rate']} months at this rate")
        if s["unfinished_runs_counted_at_upper_bound"]:
            lines.append(f"{s['unfinished_runs_counted_at_upper_bound']} run(s) died before teardown; counted at their deadline (upper bound)")
        if s["runs_in_flight"]:
            lines.append(f"{s['runs_in_flight']} detached run(s) still going; counted at their worst case "
                         f"until collected (`gpu-ops status RUN_ID`)")
        if s["unreconciled_runs"]:
            lines.append(f"{s['unreconciled_runs']} run(s) still use estimates (run `gpu-ops spend --reconcile`)")
        if s["unreadable_records"]:
            lines.append(f"{s['unreadable_records']} record(s) could not be read and are not counted")
        for note in s["reconcile_skipped"]:
            lines.append("could not reconcile " + note)
        if s["warning"]:
            lines.append("WARNING: " + s["warning"])
        for k in ("month", "project", "gpu"):
            lines.append(f"by {k}: " + ", ".join(f"{kk} ${vv['usd']:.2f}" for kk, vv in s["by"][k].items()))
        return "\n".join(lines)
    return _finish(args, work, human)


# ---------------- entry ----------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print one JSON object on stdout")
    common.add_argument("--get", metavar="PATH[,PATH]", help="print only these result fields, as a small JSON "
                                                              "object (e.g. --get cost_estimate_usd,outputs). "
                                                              "A path that does not exist is exit 2")
    p = argparse.ArgumentParser(prog="gpu-ops", description="Rent a cloud GPU, run a job, bring results home, "
                                                             "shut it down. Agents: read AGENTS.md first.")
    p.add_argument("--version", action="version", version=f"gpu-ops {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", parents=[common], help="write default policy.toml / providers.toml (humans only)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("schema", parents=[common], help="print the job spec JSON Schema").set_defaults(fn=cmd_schema)
    s = sub.add_parser("validate", parents=[common], help="check a job spec (free)")
    s.add_argument("spec")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("pin-image", parents=[common], help="resolve an image tag to a digest-pinned ref")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_pin_image)
    s = sub.add_parser("plan", parents=[common], help="choose an offer and price the run; creates nothing (free). "
                                                       "Same as `run --dry-run`.")
    s.add_argument("spec")
    s.set_defaults(fn=cmd_run, dry_run=True, progress=False, full=False, detach=False)
    s = sub.add_parser("run", parents=[common], help="run a job (COSTS MONEY unless --dry-run)")
    s.add_argument("spec")
    s.add_argument("--dry-run", action="store_true", help="plan and price only; create nothing")
    s.add_argument("--detach", action="store_true",
                   help="start the job on the instance and return at once with its run id. The machine "
                        "stays alive: finish with `gpu-ops collect RUN_ID` (or `wait`) before the "
                        "collection window closes, or the outputs are lost")
    s.add_argument("--full", action="store_true", help="print the whole run record instead of the compact summary")
    s.add_argument("--progress", action="store_true", help="stream phase lines and the job's output to stderr "
                                                            "(job.log is written either way)")
    s.add_argument("--quiet", action="store_true", help="accepted for compatibility; output is quiet by default")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status", parents=[common],
                       help="what a detached run is doing, in one small object (free)")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("collect", parents=[common],
                       help="bring a detached run's outputs home and delete the machine; "
                            "exit 11 if the job is still running")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_collect)
    s = sub.add_parser("wait", parents=[common],
                       help="block until a detached run finishes, then collect it (streams nothing)")
    s.add_argument("run_id")
    s.add_argument("--timeout", type=float, default=0,
                   help="give up waiting after this many seconds and exit 11 (default: wait for the "
                        "collection window)")
    s.add_argument("--poll", type=float, default=15, help="seconds between checks (default 15)")
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("logs", parents=[common], help="print lines from a finished run's job.log (free)")
    s.add_argument("run_id")
    s.add_argument("--tail", type=int, default=DEFAULT_LOG_TAIL,
                   help=f"last N matching lines (default {DEFAULT_LOG_TAIL}; 0 = all of them)")
    s.add_argument("--grep", metavar="REGEX", help="only lines matching this regex")
    s.add_argument("--phase", choices=["setup", "command"], help="only lines from this phase")
    s.set_defaults(fn=cmd_logs)
    sub.add_parser("ps", parents=[common], help="list running gpu-ops instances (asks providers)").set_defaults(fn=cmd_ps)
    s = sub.add_parser("kill", parents=[common], help="terminate gpu-ops instances")
    s.add_argument("ids", nargs="*")
    s.add_argument("--all", action="store_true", help="every gpuops-* instance on every provider")
    s.set_defaults(fn=cmd_kill)
    s = sub.add_parser("reap", parents=[common], help="terminate gpu-ops instances past their deadline")
    s.add_argument("--grace-min", type=float, default=10)
    s.add_argument("--max-age-min", type=float, default=0, help="for names without a deadline (default: policy "
                                                                    "max_job_minutes + grace)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_reap)
    s = sub.add_parser("spend", parents=[common], help="spend and usage report")
    s.add_argument("--reconcile", action="store_true", help="fetch provider-billed costs for past runs")
    s.set_defaults(fn=cmd_spend)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except GpuOpsError as e:
        if getattr(args, "json", False) or getattr(args, "get", None):
            print(dumps(result(e.code, error=e.message)))
        _err(f"error: {e.message}")
        return int(e.code)
    except Exception as e:  # a gpu-ops bug: still answer in the documented shape
        msg = f"internal error: {type(e).__name__}: {e}"
        if getattr(args, "json", False) or getattr(args, "get", None):
            print(dumps(result(ExitCode.INTERNAL, error=msg)))
        _err(msg)
        return int(ExitCode.INTERNAL)


if __name__ == "__main__":
    sys.exit(main())
