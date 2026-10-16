"""Console entry point: dispatches `run` and `dashboard` subcommands."""

import argparse

from . import compare, dashboard, run


def main():
    parser = argparse.ArgumentParser(prog="integration-dashboard")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser(
        "run",
        help="Run one or more (variant, python) cells of the matrix.",
        description=run.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_arguments(run_p)

    dash_p = sub.add_parser(
        "dashboard",
        help="Build the dashboard HTML from results/*.json.",
        description=dashboard.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dashboard.add_arguments(dash_p)

    compare_p = sub.add_parser(
        "compare",
        help="Compare downstream test results before and after a change to one package.",
        description=compare.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    compare.add_arguments(compare_p)

    args = parser.parse_args()
    if args.cmd == "run":
        run.run(args)
    elif args.cmd == "dashboard":
        dashboard.run(args)
    elif args.cmd == "compare":
        compare.run_compare(args)
