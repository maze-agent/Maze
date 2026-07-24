"""Offline-only ``maze-bench`` command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from ascend_maze import __version__
from ascend_maze.benchmark.loader import load_study_plan
from ascend_maze.core.errors import ContractValidationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maze-bench",
        description="Ascend-Maze deterministic experiment planner",
    )
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    plan = commands.add_parser("plan", help="validate and expand an ExperimentSpec")
    plan.add_argument("spec")
    run = commands.add_parser("run", help="execute a frozen Study plan")
    run.add_argument("spec")
    run.add_argument("--output-root", default="experiment_output")
    resume = commands.add_parser("resume", help="resume an interrupted Study")
    resume.add_argument("study_directory")
    validate = commands.add_parser(
        "validate", help="import and validate a completed Study"
    )
    validate.add_argument("study_directory")
    aggregate = commands.add_parser(
        "aggregate", help="compute deterministic Study metrics and statistics"
    )
    aggregate.add_argument("study_directory")
    report = commands.add_parser(
        "report", help="generate the machine report and offline-derived views"
    )
    report.add_argument("study_directory")
    admit = commands.add_parser(
        "admit", help="run read-only C14E Ascend environment admission"
    )
    admit.add_argument("spec")
    prepare = commands.add_parser(
        "prepare-14e", help="freeze C14E Qwen3-4B ExperimentSpec files"
    )
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--output-directory", required=True)
    prepare.add_argument("--study-kind", choices=("pilot", "formal"), required=True)
    prepare.add_argument("--rate", action="append", type=float, dest="rates")
    microbenchmark = commands.add_parser(
        "microbenchmark", help="run formal C14E component microbenchmarks"
    )
    microbenchmark.add_argument("--output-root", required=True)
    microbenchmark.add_argument(
        "--suite",
        action="append",
        choices=("c7", "c8", "c12", "c13"),
        dest="suites",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.version:
            print(f"Ascend-Maze benchmark {__version__} schema=1")
            return 0
        if args.command == "plan":
            plan = load_study_plan(args.spec)
            sys.stdout.write(plan.canonical_bytes.decode("utf-8") + "\n")
            return 0
        if args.command in {"run", "resume"}:
            result = _run_or_resume(args)
            json.dump(
                result,
                sys.stdout,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            sys.stdout.write("\n")
            return 0 if result.get("state") == "completed" else 1
        if args.command == "validate":
            from ascend_maze.benchmark.importer import validate_study

            result = validate_study(args.study_directory)
            json.dump(
                result,
                sys.stdout,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            sys.stdout.write("\n")
            return 0 if result.get("study_valid") is True else 1
        if args.command == "aggregate":
            from ascend_maze.benchmark.aggregation import aggregate_study

            result = aggregate_study(args.study_directory)
            _write_result(result)
            return 0
        if args.command == "report":
            from ascend_maze.benchmark.reporting import report_study

            result = report_study(args.study_directory)
            _write_result(result)
            return 0
        if args.command == "admit":
            from ascend_maze.benchmark.admission import collect_ascend_admission

            plan = load_study_plan(args.spec)
            _write_result(collect_ascend_admission(plan.spec).canonical_payload())
            return 0
        if args.command == "prepare-14e":
            from ascend_maze.benchmark.calibration import (
                DEFAULT_C14E_RATES,
                prepare_c14e_specs,
                spec_bundle_digest,
            )

            paths = prepare_c14e_specs(
                base_config=args.config,
                output_directory=args.output_directory,
                study_kind=args.study_kind,
                rates=DEFAULT_C14E_RATES if args.rates is None else args.rates,
            )
            _write_result(
                {
                    "schema_version": 1,
                    "study_kind": args.study_kind,
                    "spec_paths": [str(path) for path in paths],
                    "spec_bundle_digest": spec_bundle_digest(paths),
                }
            )
            return 0
        if args.command == "microbenchmark":
            import asyncio

            from ascend_maze.benchmark.microbenchmarks import (
                MICROBENCHMARK_SUITES,
                microbenchmark_result_payload,
                run_microbenchmark_suites,
            )

            results = asyncio.run(
                run_microbenchmark_suites(
                    args.output_root,
                    suites=(
                        MICROBENCHMARK_SUITES
                        if args.suites is None
                        else tuple(args.suites)
                    ),
                )
            )
            _write_result(microbenchmark_result_payload(results))
            return 0
        parser.print_help(sys.stderr)
        return 2
    except ContractValidationError as exc:
        json.dump(
            {
                "schema_version": 1,
                "status": "error",
                "error_code": "experiment_validation_failed",
                "message": str(exc),
            },
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stderr.write("\n")
        return 2
    except (TimeoutError, OSError, RuntimeError) as exc:
        json.dump(
            {
                "schema_version": 1,
                "status": "error",
                "error_code": "experiment_execution_failed",
                "message": str(exc),
            },
            sys.stderr,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        sys.stderr.write("\n")
        return 1


def _run_or_resume(args: argparse.Namespace) -> dict[str, object]:
    import asyncio

    from ascend_maze.benchmark.admission import AscendAdmissionGate
    from ascend_maze.benchmark.c13_runtime import C13BenchmarkRuntimeFactory
    from ascend_maze.benchmark.orchestrator import resume_study, run_study

    factory = C13BenchmarkRuntimeFactory(admission_gate=AscendAdmissionGate())
    if args.command == "run":
        result = asyncio.run(
            run_study(
                args.spec,
                runtime_factory=factory,
                output_root=args.output_root,
            )
        )
    else:
        result = asyncio.run(
            resume_study(args.study_directory, runtime_factory=factory)
        )
    return result.canonical_payload()


def _write_result(result: dict[str, object]) -> None:
    json.dump(
        result,
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
