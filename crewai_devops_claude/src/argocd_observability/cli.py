"""``argocd-observe`` CLI.

  argocd-observe --app NAME [--app NAME2]     diagnose one or more apps (LLM agent)
  argocd-observe --all [--project P]          health overview of every app (LLM agent)
  argocd-observe --check                      auth mode, permissions, data sources (no LLM)
  argocd-observe --all --no-llm               deterministic rule-based triage (no LLM)

Options: --mode auto|kube|argocd, --out-dir DIR (report.json + report.md), --format md|json, -v.
Exit codes: 0 all healthy, 1 at least one app needs attention, 2 error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .models import ArgoCDHealthReport
from .report import Scope, render_markdown


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="argocd-observe", description="Read-only Argo CD observability agent")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--app", action="append", metavar="NAME", help="application to diagnose (repeatable)")
    g.add_argument("--all", action="store_true", help="all applications (optionally --project)")
    g.add_argument("--check", action="store_true", help="only report auth mode, permissions and data sources")
    p.add_argument("--project", help="limit --all to an Argo CD project")
    p.add_argument("--mode", choices=["auto", "kube", "argocd"], help="auth mode (default: ARGOCD_OBS_AUTH_MODE or auto)")
    p.add_argument("--no-llm", action="store_true", help="deterministic rule-based triage, no LLM calls")
    p.add_argument("--llm-profile", metavar="NAME",
                   help="crew-wide LLM profile to use (SRE_LLM_<NAME>_*), e.g. local or openai "
                        "(default: SRE_LLM_PROFILE_ARGOCD_OBSERVABILITY, then SRE_LLM_PROFILE)")
    p.add_argument("--out-dir", default=None, help="write report.json and report.md here (default: runs/argocd-<ts>)")
    p.add_argument("--no-save", action="store_true", help="do not write report files")
    p.add_argument("--format", choices=["md", "json"], default="md", help="stdout format")
    p.add_argument("-v", "--verbose", action="store_true", help="verbose CrewAI output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass

    from .context import create_context, finalize_report
    from .settings import ObservabilitySettings

    from sre_common.llm import resolve_profile

    from .agent import AGENT_NAME

    overrides: dict = {"auth_mode": args.mode} if args.mode else {}
    if args.llm_profile:
        overrides["llm_profile"] = args.llm_profile
    try:
        settings = ObservabilitySettings(**overrides)
        llm_profile = None if args.no_llm else resolve_profile(AGENT_NAME, settings.llm_profile)
    except ValueError as exc:  # undefined profile: fail before touching the cluster
        print(f"error: {exc}", file=sys.stderr)
        return 2
    llm_desc = "none (--no-llm)" if args.no_llm else (llm_profile.describe() if llm_profile else "CrewAI default model")
    from .timing import RunTimer

    timer = RunTimer()
    try:
        with timer.phase("init", "backend and metrics sources"):
            ctx = create_context(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot initialise backend: {exc}", file=sys.stderr)
        return 2
    ctx.timer = timer

    if args.check:
        rep = ctx.access_report()
        rep["llm"] = llm_desc
        print(json.dumps(rep, indent=2, default=str))
        return 0
    print(f"[llm: {llm_desc}]", file=sys.stderr)

    scope = Scope(apps=args.app or [], project=args.project, all=bool(args.all))
    try:
        with timer.phase("preflight", "list applications in scope"):
            missing = preflight_scope(ctx, scope)
    except ScopeEmpty as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - e.g. Argo CD unreachable: fail before spending LLM tokens
        msg = f"error: cannot list applications ({exc.__class__.__name__}: {str(exc)[:200]})"
        if ctx.mode == "argocd" and "connect" in f"{exc.__class__.__name__} {exc}".lower():
            msg += (f"\nhint: is the port-forward to {settings.argocd_base_url} running? e.g. "
                    "`make argocd-ui ARGOCD_UI_PORT=18081` in k8s-lab/kind, or use --mode kube")
        print(msg, file=sys.stderr)
        return 2
    if missing:
        print(f"warning: application(s) not found, skipped: {', '.join(missing)}", file=sys.stderr)
        scope = Scope(apps=[a for a in scope.apps if a not in missing])
        ctx.startup_warnings.append(f"requested application(s) not found and skipped: {', '.join(missing)}")
    try:
        if args.no_llm:
            from .triage import run_triage

            with timer.phase("triage", "rule-based triage (no LLM)"):
                triaged = run_triage(ctx, scope)
            report = finalize_report(triaged, ctx, scope.describe())
        else:
            from .crew import run_observability

            report = run_observability(scope, settings, context=ctx, verbose=args.verbose)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    _emit(report, args)
    return 1 if report.unhealthy else 0


class ScopeEmpty(LookupError):
    """Nothing in scope exists: stop before any LLM call."""


def preflight_scope(ctx, scope: Scope) -> list[str]:
    """One read-only list call before the agent runs.

    Returns the requested app names that do not exist (the run continues with the rest).
    Raises ScopeEmpty when nothing in scope exists, so a typo or a demo app that was never created
    costs one API call instead of an LLM run that can only report "not found".
    """
    apps = ctx.backend.list_applications(project=scope.project)
    names = {a.get("metadata", {}).get("name") for a in apps}
    if scope.apps:
        missing = [a for a in scope.apps if a not in names]
        if len(missing) == len(scope.apps):
            hint = f" (existing: {', '.join(sorted(n for n in names if n)[:15])})" if names else ""
            raise ScopeEmpty(f"none of the requested applications exist in {ctx.backend.describe()}: "
                             f"{', '.join(missing)}{hint}")
        return missing
    if not names:
        where = f"project '{scope.project}'" if scope.project else "Argo CD"
        raise ScopeEmpty(f"no applications found in {where} ({ctx.backend.describe()})")
    return []


def _emit(report: ArgoCDHealthReport, args: argparse.Namespace) -> None:
    md = render_markdown(report)
    if not args.no_save:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = Path(args.out_dir or f"runs/argocd-{stamp}")
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(report.model_dump_json(indent=2))
        (out / "report.md").write_text(md)
        print(f"[saved {out}/report.json and report.md]", file=sys.stderr)
    print(report.model_dump_json(indent=2) if args.format == "json" else md)


if __name__ == "__main__":
    sys.exit(main())
