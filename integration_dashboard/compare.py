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

A local path is built into a wheel once at the start, so every package is
tested against the same build of its current contents.

Only the changed package differs between the two runs, so comparing the
individual test results shows what the change broke (or fixed), even when a
downstream package already has failing tests on its own.

The output shows the full traceback of every newly failing test, then a
one-line summary per package. Exits non-zero if any test newly fails, or if
either environment fails to install, since the change could then not be
checked.

Usage:
    integration-dashboard compare --package astropy \\
        --before git+https://github.com/astropy/astropy@<base commit> \\
        --after /path/to/astropy-checkout --tiers coordinated
"""

import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from packaging.utils import canonicalize_name

from . import config, run, status

# Outcomes that count as a regression when a test had passed before.
BROKEN = ("failed", "error")
OUTCOME = {"failure": "failed", "error": "error", "skipped": "skipped"}


def _name(requirement_line):
    """Canonical package name of a ``name==version`` or ``name @ url`` line."""
    name = requirement_line.split("==", 1)[0].split(" @ ", 1)[0].split("[", 1)[0]
    return canonicalize_name(name.strip())


def _test_outcomes(junit_path):
    """{test id: outcome} and {test id: traceback} from a JUnit file.

    Test ids are made venv-independent by dropping everything up to
    ``site-packages``, so the same test matches across the two runs.
    """
    outcomes, tracebacks = {}, {}
    if not Path(junit_path).exists():
        return outcomes, tracebacks
    for case in ET.parse(junit_path).iter("testcase"):
        classname = case.get("classname", "").split("site-packages.", 1)[-1]
        test_id = f"{classname}::{case.get('name')}"
        child = next((c for c in case if c.tag in OUTCOME), None)
        outcomes[test_id] = OUTCOME[child.tag] if child is not None else "passed"
        if child is not None and child.tag != "skipped":
            tracebacks[test_id] = child.text or child.get("message") or ""
    return outcomes, tracebacks


def _requirement(ctx, spec, label):
    """A requirement for the changed package; a local path is built into a wheel."""
    if "://" not in spec:
        out = os.path.join(ctx.workdir, f"wheel-{label}")
        print(f"Building a wheel from {spec}...")
        proc = run._uv(
            ["build", "--wheel", "--out-dir", out, "--python", ctx.python, spec],
            ctx.timeouts["install"],
        )
        wheels = list(Path(out).glob("*.whl"))
        if proc.returncode != 0 or not wheels:
            sys.exit(f"Could not build {spec}:\n{proc.stderr or proc.stdout}")
        spec = wheels[0].as_uri()
    return f"{ctx.changed} @ {spec}"


def _install(ctx, venv_dir, requirements_text):
    """Create a venv and install requirements into it. Returns (python, error)."""
    proc = run._uv(["venv", venv_dir, "-p", ctx.python, "-q"], timeout=120)
    if proc.returncode != 0:
        return None, proc.stderr or proc.stdout
    python = os.path.join(venv_dir, "bin", "python")
    reqs = os.path.join(venv_dir, "requirements.txt")
    Path(reqs).write_text(requirements_text)
    args = ["pip", "install", "--quiet", "--python", python, *ctx.options]
    args += ["-r", reqs, "--constraint", ctx.constraints_file]
    proc = run._uv(args, ctx.timeouts["install"])
    return python, ("" if proc.returncode == 0 else proc.stderr or proc.stdout)


def compare_package(pkg, ctx):
    """Test one downstream package before and after the change."""
    name = pkg["pypi_name"]
    print(f"\n=== {name} ===")
    result = {
        "name": name,
        "changed": ctx.changed,
        "before": {"spec": ctx.before_spec},
        "after": {"spec": ctx.after_spec},
        "newly_failing": [],
        "newly_passing": [],
    }
    reqs = run._requirements(pkg, ctx.variant, nightly=set())
    if reqs is None:
        for label in ("before", "after"):
            result[label]["install_error"] = "no repo_url to build the dev version from"
        return result

    # Lock the "before" environment; "after" is the same lock with the
    # changed package swapped.
    args = ["pip", "compile", "-", "--no-header", "--no-annotate", "--quiet"]
    args += ["--python", ctx.python, *ctx.options, "--constraint", ctx.constraints_file]
    stdin = "\n".join([*reqs, *ctx.infra, ctx.before_req]) + "\n"
    proc = run._uv(args, ctx.timeouts["install"], stdin=stdin)
    if proc.returncode != 0:
        result["before"]["install_error"] = proc.stderr or proc.stdout
        print(
            f"  could not resolve the before environment:\n{proc.stderr or proc.stdout}"
        )
        return result
    rest = [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and _name(line) != canonicalize_name(ctx.changed)
    ]
    locks = {"before": proc.stdout, "after": "\n".join([*rest, ctx.after_req]) + "\n"}

    pkg_dir = tempfile.mkdtemp(prefix=f"{name}-", dir=ctx.workdir)
    outcomes = {}
    tracebacks = {}
    try:
        for label, lock in locks.items():
            side = result[label]
            print(f"  building {label} venv...")
            python, error = _install(ctx, os.path.join(pkg_dir, label), lock)
            if error:
                side["install_error"] = error
                print(f"  {label} venv failed to install:\n{error}")
                continue
            side["version"] = run._installed(python).get(canonicalize_name(name), "")
            junit = os.path.join(pkg_dir, f"{label}.xml")
            side["test_status"], _, side["duration"] = run.run_tests(
                python,
                pkg["module"],
                pkg.get("pytest_args", []),
                ctx.repo_root,
                ctx.timeouts["test"],
                junit_path=junit,
                traceback="long",
            )
            outcomes[label], tracebacks = _test_outcomes(junit)
            side["counts"] = dict(Counter(outcomes[label].values()))
            print(
                f"  {label}: {side['test_status']} {side['counts']} in {side['duration']}s"
            )
    finally:
        shutil.rmtree(pkg_dir, ignore_errors=True)

    if len(outcomes) == 2:
        old, new = outcomes["before"], outcomes["after"]
        result["newly_failing"] = sorted(
            t for t, o in new.items() if o in BROKEN and old.get(t) not in BROKEN
        )
        result["newly_passing"] = sorted(
            t for t, o in new.items() if o == "passed" and old.get(t) in BROKEN
        )
        # Full tracebacks go to the output only; the summary just has counts.
        for test in result["newly_failing"]:
            print(f"\n_____ newly failing: {test} _____\n")
            print(tracebacks.get(test, "").rstrip())
        for test in result["newly_passing"]:
            print(f"  newly passing: {test}")
    return result


def _outcome(result):
    """(failed, description): did the comparison fail, and how to describe it.

    Any install failure fails it, since then the change could not be checked.
    """
    before, after = result["before"], result["after"]
    if "install_error" in before:
        return True, "could not install the before environment"
    if "install_error" in after:
        return True, "no longer installs"
    if result["newly_failing"]:
        return True, f"{len(result['newly_failing'])} newly failing"
    return False, "no new failures"


def _summary_cell(side):
    if "install_error" in side:
        return "install failed"
    if "counts" not in side:
        return "not run"
    passed = side["counts"].get("passed", 0)
    broken = sum(side["counts"].get(o, 0) for o in BROKEN)
    return f"{passed} passed, {broken} failing" if broken else f"{passed} passed"


def _summary_line(result):
    outcome = _outcome(result)[1]
    if result["newly_passing"]:
        outcome += f", {len(result['newly_passing'])} newly passing"
    before, after = _summary_cell(result["before"]), _summary_cell(result["after"])
    return f"{result['name']}: {outcome} (before: {before}; after: {after})"


def add_arguments(ap):
    ap.add_argument("--config", default="packages.yaml")
    ap.add_argument(
        "--package", required=True, help="Listed package being changed (e.g. astropy)"
    )
    ap.add_argument(
        "--before",
        required=True,
        help="pip URL or local path of the package before the change",
    )
    ap.add_argument(
        "--after",
        required=True,
        help="pip URL or local path of the package after the change",
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
    ap.add_argument("--timeout-install", type=int, default=900)
    ap.add_argument("--timeout-test", type=int, default=1800)


def run_compare(args):
    sys.stdout.reconfigure(line_buffering=True)
    changed = canonicalize_name(args.package)
    all_packages = config.load_packages(args.config)
    if changed not in {canonicalize_name(p["pypi_name"]) for p in all_packages}:
        sys.exit(f"--package {args.package} is not listed in {args.config}")
    packages = [
        p
        for p in run.select_packages(all_packages, args.tiers, args.packages)
        if canonicalize_name(p["pypi_name"]) != changed
    ]
    if not packages:
        sys.exit("No downstream packages selected.")

    settings = config.load_settings(args.config)
    Path(".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=Path(".tmp").resolve()) as workdir:
        constraints_file = os.path.join(workdir, "constraints.txt")
        Path(constraints_file).write_text(
            "".join(
                c["spec"] + "\n"
                for c in settings["constraints"]
                if args.variant in c["variants"]
            )
        )
        ctx = SimpleNamespace(
            changed=args.package,
            before_spec=args.before,
            after_spec=args.after,
            variant=args.variant,
            python=run.ensure_python(args.python),
            options=run.uv_options(args.variant, settings["dev_index_urls"]),
            constraints_file=constraints_file,
            infra=run.TEST_INFRA + settings["test_deps"],
            timeouts={"install": args.timeout_install, "test": args.timeout_test},
            repo_root=Path.cwd(),
            workdir=workdir,
        )
        ctx.before_req = _requirement(ctx, args.before, "before")
        ctx.after_req = _requirement(ctx, args.after, "after")
        results = [compare_package(pkg, ctx) for pkg in packages]

    print("\n===== Summary =====")
    for result in results:
        print(_summary_line(result))
    if any(_outcome(r)[0] for r in results):
        sys.exit(1)
