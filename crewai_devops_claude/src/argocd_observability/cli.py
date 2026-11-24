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

    overrides = {"auth_mode": args.mode} if args.mode else {}
    try:
        settings = ObservabilitySettings(**overrides)
        ctx = create_context(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot initialise backend: {exc}", file=sys.stderr)
        return 2

    if args.check:
        rep = ctx.access_report()
        print(json.dumps(rep, indent=2, default=str))
        return 0

    scope = Scope(apps=args.app or [], project=args.project, all=bool(args.all))
    try:
        if args.no_llm:
            from .triage import run_triage

            report = finalize_report(run_triage(ctx, scope), ctx, scope.describe())
        else:
            from .crew import run_observability

            report = run_observability(scope, settings, context=ctx, verbose=args.verbose)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2

    _emit(report, args)
    return 1 if report.unhealthy else 0


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
