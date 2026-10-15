"""Compare downstream test results before and after a change to one package.

Meant for checking a pull request to a package that others depend on (for
example astropy): ``--package`` names the listed package being changed, and
``--before`` / ``--after`` give it as a pip URL (``git+https://...@<commit>``)
or a local path. Every other selected package is tested twice, each time in
its own venv:

  1. "before": the downstream package and its dependencies are resolved with
     the changed package at ``--before``, the result is locked (git installs
     are locked to a commit), and the venv is built from that lock.
  2. "after": the same lock, except that the changed package comes from
     ``--after``. Anything the new version needs that the lock cannot
     provide is resolved fresh.

Only the changed package differs between the two runs, so comparing the
individual test results shows what the change broke (or fixed), even when a
downstream package already has failing tests on its own.

Writes one ``<package>.json`` per downstream package to ``--results-dir``
and a Markdown report to ``--report`` from every JSON there (with
``--report-only``, just the report). Exits non-zero if any test newly fails,
or a package that installed before no longer installs.

Usage:
    integration-dashboard compare --package astropy \\
        --before git+https://github.com/astropy/astropy@<base commit> \\
        --after /path/to/astropy-checkout --tiers coordinated
"""

import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from packaging.utils import canonicalize_name

from . import config, run, status

# Outcomes that count as a regression when a test had passed before.
BROKEN = ("failed", "error")


def _as_url(spec):
    """Turn a local path into a file URL; leave pip URLs alone."""
    return spec if "://" in spec else Path(spec).resolve().as_uri()


def _name(requirement_line):
    """Canonical package name of a ``name==version`` or ``name @ url`` line."""
    name = requirement_line.split("==", 1)[0].split(" @ ", 1)[0].split("[", 1)[0]
    return canonicalize_name(name.strip())


def _test_outcomes(junit_path):
    """{test id: outcome} from a JUnit file, with outcome passed/failed/error/skipped.

    Test ids are made venv-independent by dropping everything up to
    ``site-packages``, so the same test matches across the two runs.
    """
    outcomes = {}
    messages = {}
    if not Path(junit_path).exists():
        return outcomes, messages
    for case in ET.parse(junit_path).iter("testcase"):
        classname = case.get("classname", "")
        classname = classname.split("site-packages.", 1)[-1]
        test_id = f"{classname}::{case.get('name')}"
        outcome = "passed"
        for child in case:
            if child.tag in ("failure", "error", "skipped"):
                outcome = {"failure": "failed"}.get(child.tag, child.tag)
                if child.tag != "skipped":
                    messages[test_id] = (child.get("message") or child.text or "")[
                        :2000
                    ]
                break
        outcomes[test_id] = outcome
    return outcomes, messages


class Side:
    """One of the two venvs (before or after) for one downstream package."""

    def __init__(self, label, tmpdir, python, column):
        self.label = label
        self.dir = os.path.join(tmpdir, label)
        os.makedirs(self.dir)
        self.venv = os.path.join(self.dir, "venv")
        self.python = os.path.join(self.venv, "bin", "python")
        self.column = column
        self.base_python = python

    def build(self, requirements_text):
        """Create the venv and install ``requirements_text``. Returns an error or ''."""
        proc = run._uv(["venv", self.venv, "-p", self.base_python, "-q"], timeout=120)
        if proc.returncode != 0:
            return proc.stderr or proc.stdout
        reqs = os.path.join(self.dir, "requirements.txt")
        Path(reqs).write_text(requirements_text)
        # The column's options target its base interpreter; install into this venv.
        options = list(self.column.options)
        options[options.index("--python") + 1] = self.python
        args = ["pip", "install", "--quiet", *options, "-r", reqs]
        args += ["--constraint", self.column.constraints_file]
        proc = run._uv(args, self.column.timeout)
        return "" if proc.returncode == 0 else (proc.stderr or proc.stdout)


def compare_package(
    pkg, changed, before, after, cfg, variant, python_version, repo_root, timeouts
):
    """Test one downstream package before and after the change."""
    name = pkg["pypi_name"]
    print(f"\n=== {name} ===")
    result = {
        "name": name,
        "tier": pkg.get("tier", ""),
        "changed": changed,
        "before": {"spec": before},
        "after": {"spec": after},
        "newly_failing": [],
        "newly_passing": [],
    }
    reqs = run._requirements(pkg, variant, nightly=set())
    if reqs is None:
        result["error"] = "no repo_url to build the dev version from"
        return result

    Path(repo_root, ".tmp").mkdir(exist_ok=True)
    tmpdir = tempfile.mkdtemp(
        prefix=f"compare-{name}-", dir=str(Path(repo_root, ".tmp").resolve())
    )
    try:
        python = run.ensure_python(python_version)
        constraints_file = os.path.join(tmpdir, "constraints.txt")
        Path(constraints_file).write_text(
            "".join(
                c["spec"] + "\n" for c in cfg["constraints"] if variant in c["variants"]
            )
        )
        column = run.Column(
            variant,
            python,
            cfg["dev_index_urls"],
            constraints_file,
            tmpdir,
            timeouts["install"],
        )
        infra = run.TEST_INFRA + cfg["test_deps"]

        # Lock the "before" environment, then build both venvs from it.
        before_req = f"{changed} @ {_as_url(before)}"
        args = ["pip", "compile", "-", "--no-header", "--no-annotate", "--quiet"]
        args += column.options + ["--constraint", constraints_file]
        proc = run._uv(
            args, column.timeout, stdin="\n".join([*reqs, *infra, before_req]) + "\n"
        )
        if proc.returncode != 0:
            result["before"]["install_error"] = proc.stderr or proc.stdout
            print("  could not resolve the before environment:")
            print(result["before"]["install_error"])
            return result
        lock = proc.stdout
        changed_key = canonicalize_name(changed)
        rest = [
            line
            for line in lock.splitlines()
            if line.strip() and _name(line) != changed_key
        ]
        after_req = f"{changed} @ {_as_url(after)}"

        sides = {"before": lock, "after": "\n".join([*rest, after_req]) + "\n"}
        for label, requirements_text in sides.items():
            side = Side(label, tmpdir, python, column)
            print(f"  building {label} venv...")
            error = side.build(requirements_text)
            if error:
                result[label]["install_error"] = error
                print(f"  {label} venv failed to install:\n{error}")
                continue
            installed = run._installed(side.python)
            result[label]["changed_version"] = installed.get(changed_key, "")
            result[label]["version"] = installed.get(canonicalize_name(name), "")
            junit = os.path.join(side.dir, "junit.xml")
            test_status, output, duration = run.run_tests(
                side.python,
                pkg["module"],
                pkg.get("pytest_args", []),
                repo_root,
                timeouts["test"],
                junit_path=junit,
            )
            outcomes, messages = _test_outcomes(junit)
            result[label].update(
                {
                    "test_status": test_status,
                    "duration": duration,
                    "outcomes": outcomes,
                    "messages": messages,
                    "output_tail": output[-3000:],
                }
            )
            counts = {
                o: sum(v == o for v in outcomes.values())
                for o in ("passed", "failed", "error", "skipped")
            }
            result[label]["counts"] = counts
            print(f"  {label}: {test_status} {counts} in {duration}s")

        if "outcomes" in result["before"] and "outcomes" in result["after"]:
            old, new = result["before"]["outcomes"], result["after"]["outcomes"]
            result["newly_failing"] = sorted(
                test
                for test, outcome in new.items()
                if outcome in BROKEN and old.get(test) not in BROKEN
            )
            result["newly_passing"] = sorted(
                test
                for test, outcome in new.items()
                if outcome == "passed" and old.get(test) in BROKEN
            )
            # The details go to the log only; the report just has the counts.
            print(f"  newly failing: {len(result['newly_failing'])}")
            for test in result["newly_failing"]:
                print(f"    {test}")
                message = result["after"]["messages"].get(test, "").strip()
                if message:
                    print("      " + message.replace("\n", "\n      "))
            print(f"  newly passing: {len(result['newly_passing'])}")
            for test in result["newly_passing"]:
                print(f"    {test}")
        # Keep the JSON small: the per-test outcomes were only needed for the diff.
        for label in ("before", "after"):
            result[label].pop("outcomes", None)
            result[label].pop("messages", None)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return result


def _regressed(result):
    if result["newly_failing"]:
        return True
    return (
        "install_error" in result["after"] and "install_error" not in result["before"]
    )


def _summary_cell(result, label):
    side = result[label]
    if result.get("error"):
        return "not tested"
    if "install_error" in side:
        return "install failed"
    if "counts" not in side:
        return "-"
    c = side["counts"]
    broken = c["failed"] + c["error"]
    return (
        f"{c['passed']} passed, {broken} failing" if broken else f"{c['passed']} passed"
    )


def render_report(results):
    """Markdown summary table of every compared package.

    Which tests newly fail, and why, is left to the log of each package's run.
    """
    if not results:
        return "No packages were compared.\n"
    first = results[0]
    regressed = [r for r in results if _regressed(r)]
    lines = [
        f"## Downstream tests: `{first['changed']}` before and after",
        "",
        f"- before: `{first['before']['spec']}`",
        f"- after: `{first['after']['spec']}`",
        "",
        f"**{len(regressed)} of {len(results)} packages have new failures.**"
        if regressed
        else f"**No new failures in {len(results)} packages.**",
        "",
        "| Package | Before | After | Newly failing | Newly passing |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        version = r["after"].get("version") or r["before"].get("version", "")
        failing = len(r["newly_failing"])
        lines.append(
            f"| {r['name']} {version} | {_summary_cell(r, 'before')} "
            f"| {_summary_cell(r, 'after')} | {f'**{failing}**' if failing else ''} "
            f"| {len(r['newly_passing']) or ''} |"
        )
    run_url = "{GITHUB_SERVER_URL}/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}"
    try:
        where = f"the [workflow run]({run_url.format(**os.environ)})"
    except KeyError:
        where = "the output of `integration-dashboard compare`"
    lines += [
        "",
        f"The failing tests and install errors for each package are in {where}.",
    ]
    return "\n".join(lines) + "\n"


def add_arguments(ap):
    ap.add_argument("--config", default="packages.yaml")
    ap.add_argument(
        "--package", required=True, help="Listed package being changed (e.g. astropy)"
    )
    ap.add_argument(
        "--before", help="pip URL or local path of the package before the change"
    )
    ap.add_argument(
        "--after", help="pip URL or local path of the package after the change"
    )
    ap.add_argument("--packages", help="Comma-separated downstream packages to test")
    ap.add_argument(
        "--tiers", help="Comma-separated tiers of downstream packages to test"
    )
    ap.add_argument(
        "--variant",
        choices=status.VARIANTS,
        default="dev",
        help="How downstream packages are installed (default: dev, i.e. their main branch)",
    )
    ap.add_argument("--python", default="3.12")
    ap.add_argument("--results-dir", default="compare-results")
    ap.add_argument("--report", default="compare-report.md")
    ap.add_argument(
        "--report-only",
        action="store_true",
        help="Only render the report from the JSON files already in --results-dir",
    )
    ap.add_argument("--timeout-install", type=int, default=900)
    ap.add_argument("--timeout-test", type=int, default=1800)


def run_compare(args):
    sys.stdout.reconfigure(line_buffering=True)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if not args.report_only:
        if not (args.before and args.after):
            sys.exit("--before and --after are required unless --report-only is given")
        packages = config.load_packages(args.config)
        changed = canonicalize_name(args.package)
        if changed not in {canonicalize_name(p["pypi_name"]) for p in packages}:
            sys.exit(f"--package {args.package} is not listed in {args.config}")
        packages = [p for p in packages if canonicalize_name(p["pypi_name"]) != changed]
        if args.tiers:
            tiers = {t.strip() for t in args.tiers.split(",")}
            packages = [p for p in packages if p.get("tier", "") in tiers]
        if args.packages:
            wanted = {
                canonicalize_name(n) for n in args.packages.split(",") if n.strip()
            }
            packages = [
                p for p in packages if canonicalize_name(p["pypi_name"]) in wanted
            ]
        if not packages:
            sys.exit("No downstream packages selected.")
        cfg = {
            "constraints": config.load_constraints(args.config),
            "dev_index_urls": config.load_dev_index_urls(args.config),
            "test_deps": config.load_test_deps(args.config),
        }
        timeouts = {"install": args.timeout_install, "test": args.timeout_test}
        for pkg in packages:
            result = compare_package(
                pkg,
                args.package,
                args.before,
                args.after,
                cfg,
                args.variant,
                args.python,
                Path.cwd(),
                timeouts,
            )
            out = results_dir / f"{canonicalize_name(pkg['pypi_name'])}.json"
            out.write_text(json.dumps(result, indent=2))

    results = [json.loads(f.read_text()) for f in sorted(results_dir.glob("*.json"))]
    report = render_report(results)
    Path(args.report).write_text(report)
    print(f"\nWrote {args.report}")
    if any(_regressed(r) for r in results):
        sys.exit(1)
