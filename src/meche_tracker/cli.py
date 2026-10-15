"""Public command line interface; local commands never commit or push."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .models import ATS_LABELS, utc_now
from .pipeline import collect, refresh, render
from .query import REGIONS, Query, select
from .registry import load_employers
from .render import table
from .store import apply_overrides, json_text, load_state


def _boolean(value: str) -> bool:
    if value not in {"yes", "no"}:
        raise argparse.ArgumentTypeError("value must be yes or no")
    return value == "yes"


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def parser() -> argparse.ArgumentParser:
    app = argparse.ArgumentParser(prog="meche-tracker", description=__doc__)
    app.add_argument("--root", type=Path, default=Path.cwd(), help="project directory")
    app.add_argument("--verbose", action="store_true")
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate employer configurations and canonical state")
    commands.add_parser("render", help="regenerate views without network access")
    refresh_parser = commands.add_parser("refresh", help="collect and publish local files")
    refresh_parser.add_argument("--employer", action="append", default=[])
    refresh_parser.add_argument("--dry-run", action="store_true")
    refresh_parser.add_argument("--force-details", action="store_true")
    sources = commands.add_parser("sources", help="inspect employer source connectivity")
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    check = source_commands.add_parser("check")
    check.add_argument("--employer", action="append", default=[])
    check.add_argument("--include-disabled", action="store_true")
    query = commands.add_parser("query", help="combine filters; multiple values use OR")
    for field in ["employer", "industry", "state", "specialty", "degree"]:
        query.add_argument(f"--{field}", action="append", default=[])
    query.add_argument(
        "--platform",
        "--ats",
        choices=list(ATS_LABELS),
        action="append",
        default=[],
        help="filter by ATS platform; repeat to include multiple platforms",
    )
    query.add_argument("--region", choices=list(REGIONS), action="append", default=[])
    query.add_argument(
        "--workplace",
        choices=["onsite", "hybrid", "remote", "unknown"],
        action="append",
        default=[],
    )
    query.add_argument("--type", choices=["internship", "co-op"], action="append", default=[])
    query.add_argument(
        "--season",
        "--term",
        choices=["spring", "summer", "fall", "winter"],
        action="append",
        default=[],
    )
    query.add_argument("--year", type=int, action="append", default=[])
    query.add_argument("--kind", choices=["role", "early_interest", "talent_pool"], action="append")
    query.add_argument(
        "--status", choices=["open", "closed", "suspected_closed", "unverified"], action="append"
    )
    query.add_argument("--decision", choices=["include", "review", "exclude"], action="append")
    query.add_argument("--include-graduate", action="store_true")
    query.add_argument("--graduate-only", action="store_true")
    query.add_argument("--include-stale", action="store_true")
    query.add_argument("--discovered-days", type=int)
    query.add_argument("--verified-hours", type=float, default=48)
    query.add_argument("--pay-available", type=_boolean, metavar="{yes,no}")
    query.add_argument("--minimum-pay", type=float)
    query.add_argument("--pay-unit", choices=["hour", "year", "month", "week"])
    query.add_argument("--pay-currency")
    for field in ["sponsorship", "housing", "relocation"]:
        query.add_argument(f"--{field}", type=_boolean, metavar="{yes,no}")
    query.add_argument("--start-after", type=_date)
    query.add_argument("--end-before", type=_date)
    query.add_argument("--minimum-weeks", type=float)
    query.add_argument("--maximum-weeks", type=float)
    query.add_argument("--sort", choices=["discovered", "posted"], default="discovered")
    query.add_argument("--format", choices=["json", "markdown"], default="json")
    return app


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    root = args.root.resolve()
    try:
        if args.command == "validate":
            employers = load_employers(root)
            catalog, health, overrides = load_state(root)
            apply_overrides(catalog, overrides)
            enabled = sum(source.enabled for employer in employers for source in employer.sources)
            print(
                json.dumps(
                    {
                        "employers": len(employers),
                        "enabled_sources": enabled,
                        "jobs": len(catalog.jobs),
                        "source_states": len(health.sources),
                    }
                )
            )
        elif args.command == "refresh":
            report = refresh(
                root, args.employer, dry_run=args.dry_run, force_details=args.force_details
            )
            print(json_text(report), end="")
        elif args.command == "render":
            render(root)
            print("Generated README and filtered views.")
        elif args.command == "sources":
            employers = load_employers(root)
            ids = set(args.employer)
            unknown = ids - {employer.id for employer in employers}
            if unknown:
                raise ValueError(f"unknown employers: {', '.join(sorted(unknown))}")
            selected = [
                employer.model_copy(deep=True)
                for employer in employers
                if not ids or employer.id in ids
            ]
            if args.include_disabled:
                for employer in selected:
                    for source in employer.sources:
                        # Source checking is read-only and may verify a not-yet-enabled board.
                        source.verified_at = source.verified_at or utc_now()
                        source.enabled = True
            catalog, _, _ = load_state(root)
            results = collect(selected, catalog, utc_now())
            print(
                json.dumps(
                    [
                        {
                            "employer": result.employer_id,
                            "source": result.source_id,
                            "complete": result.complete,
                            "inventory": len(result.items),
                            "details": len(result.postings),
                            "errors": result.errors,
                            "detail_errors": result.detail_errors,
                        }
                        for result in results
                    ],
                    indent=2,
                )
            )
            if not results or any(not result.complete for result in results):
                return 1
        elif args.command == "query":
            catalog, _, _ = load_state(root)
            query = Query(
                employers=args.employer,
                platforms=args.platform,
                industries=args.industry,
                states=[state.upper() for state in args.state],
                regions=args.region,
                workplaces=args.workplace,
                types=args.type,
                seasons=args.season,
                years=args.year,
                specialties=args.specialty,
                degrees=args.degree,
                kinds=args.kind or ["role"],
                statuses=args.status or ["open"],
                decisions=args.decision or ["include"],
                include_graduate=args.include_graduate or args.graduate_only,
                graduate_only=args.graduate_only,
                include_stale=args.include_stale,
                discovered_days=args.discovered_days,
                verified_hours=args.verified_hours,
                pay_available=args.pay_available,
                minimum_pay=args.minimum_pay,
                pay_unit=args.pay_unit,
                pay_currency=args.pay_currency,
                sponsorship=args.sponsorship,
                housing=args.housing,
                relocation=args.relocation,
                start_after=args.start_after,
                end_before=args.end_before,
                minimum_weeks=args.minimum_weeks,
                maximum_weeks=args.maximum_weeks,
                sort=args.sort,
            )
            if query.verified_hours < 0 or (
                query.discovered_days is not None and query.discovered_days < 0
            ):
                raise ValueError("recency windows must be nonnegative")
            jobs = select(catalog.jobs, query, utc_now())
            print(
                table(jobs)
                if args.format == "markdown"
                else json_text([job.model_dump(mode="json") for job in jobs]),
                end="",
            )
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
