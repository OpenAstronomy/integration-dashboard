"""Run one or more columns of the ecosystem integration matrix.

Every listed package follows the column's variant:

  - stable: latest release
  - pre: latest release or pre-release
  - dev: a nightly wheel from ``dev_index_urls`` if one is available,
    otherwise the HEAD of the package's ``repo_url``

All listed packages share one venv, and no package may drag a shared
dependency down for the others. Each listed package is first resolved on
its own. Then, for every listed package and every direct dependency of a
listed package, the highest version any of those resolutions reached
becomes a minimum, and each package is resolved again with those
minimums. A package that resolves on its own but not with the minimums
needs an older version of something another package uses, so it is
reported as held back, with uv's explanation, and is not installed.
Dependencies further down the tree are left to uv, since their caps come
from packages outside the list. The config's ``constraints`` apply
throughout. The remaining packages are installed into one
shared venv and each is tested with ``pytest --pyargs <module>``.

Each column writes results/<variant>__<python>.json.

A "column" is one (python, variant) pair from the config; ``--variant``
and ``--python`` narrow the configured columns down to a subset.

Usage:
    integration-dashboard run                                    # every configured column
    integration-dashboard run --variant stable                   # configured columns with variant=stable
    integration-dashboard run --variant stable --python 3.12     # one specific column
    integration-dashboard run --variant stable --packages foo,bar
    integration-dashboard run --variant stable --tiers coordinated
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import Version

from . import config, status

# Always installed alongside each column's test_deps.
TEST_INFRA = ["pytest", "pytest-timeout"]


def _uv(args, timeout, stdin=None):
    """Run a uv command, with colour off so its output can be parsed."""
    try:
        return subprocess.run(
            ["uv", "--color", "never", *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args, 1, "", f"uv timed out after {timeout}s"
        )


def ensure_python(version):
    proc = _uv(["python", "find", version], timeout=60)
    if proc.returncode != 0:
        inst = _uv(["python", "install", version], timeout=600)
        if inst.returncode != 0:
            sys.exit(f"uv python install {version}: {inst.stderr.strip()}")
        proc = _uv(["python", "find", version], timeout=60)
    if proc.returncode != 0:
        sys.exit(f"uv python find {version}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _venv_python_version(python):
    proc = subprocess.run(
        [python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _parse_compile(text):
    """Parse ``uv pip compile --annotation-style split`` output.

    Returns {canonical name: (version, {names of the packages that require
    it})}. Top-level requirements have no requirers, and a package
    installed from a URL (``name @ git+...``) has version None.
    """
    pins = {}
    name = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            via = line.lstrip("#").strip().removeprefix("via").strip()
            if name and via:
                pins[name][1].update(
                    canonicalize_name(v.strip()) for v in via.split(",")
                )
            continue
        if " @ " in line:
            raw, version = line.split(" @ ", 1)[0], None
        elif "==" in line:
            raw, version = (part.strip() for part in line.split("==", 1))
        else:
            continue
        name = canonicalize_name(raw.split("[", 1)[0])
        pins[name] = (version, set())
    return pins


def _installed(python):
    """{canonical name: version} for everything installed in the venv."""
    proc = _uv(["pip", "list", "--python", python, "--format", "json"], timeout=60)
    if proc.returncode != 0:
        return {}
    return {canonicalize_name(p["name"]): p["version"] for p in json.loads(proc.stdout)}


def uv_options(variant, dev_index_urls):
    """uv resolve/install options for a variant (everything except --python)."""
    options = []
    if variant in ("pre", "dev"):
        options += ["--prerelease", "allow"]
    if variant == "dev" and dev_index_urls:
        for url in dev_index_urls:
            options += ["--extra-index-url", url]
        # A nightly channel only hosts some packages and its wheels may
        # not cover this platform, so let uv pick the best match across
        # every index rather than stopping at the first that has a name.
        options += ["--index-strategy", "unsafe-best-match"]
    return options


def select_packages(packages, tiers=None, names=None):
    """Filter packages by comma-separated tiers and names; exit on unknown names."""
    if names:
        wanted = {canonicalize_name(n) for n in names.split(",") if n.strip()}
        unknown = wanted - {canonicalize_name(p["pypi_name"]) for p in packages}
        if unknown:
            sys.exit(
                f"Unknown package name(s): {', '.join(sorted(unknown))}. "
                "Valid names are the pypi_name entries in the config."
            )
        packages = [p for p in packages if canonicalize_name(p["pypi_name"]) in wanted]
    if tiers:
        wanted_tiers = {t.strip() for t in tiers.split(",") if t.strip()}
        packages = [p for p in packages if p.get("tier", "") in wanted_tiers]
    return packages


class Column:
    """uv settings shared by every resolve and install in one column."""

    def __init__(
        self, variant, python, dev_index_urls, constraints_file, tmpdir, timeout
    ):
        self.timeout = timeout
        self.constraints_file = constraints_file
        self.floors_file = os.path.join(tmpdir, "floors.txt")
        self.options = ["--python", python, *uv_options(variant, dev_index_urls)]

    def resolve(self, requirements, floors=True, no_deps=False):
        """Resolve requirements without installing. Returns (pins, error)."""
        args = ["pip", "compile", "-", "--no-header", "--quiet"]
        args += ["--annotation-style", "split"]
        args += self.options + ["--constraint", self.constraints_file]
        if floors:
            args += ["--constraint", self.floors_file]
        if no_deps:
            args.append("--no-deps")
        proc = _uv(args, self.timeout, stdin="\n".join(requirements) + "\n")
        if proc.returncode != 0:
            return None, proc.stderr or proc.stdout
        return _parse_compile(proc.stdout), ""

    def install(self, requirements):
        """Install requirements into the venv at the planned versions."""
        args = ["pip", "install", "--quiet", *self.options]
        args += ["--constraint", self.constraints_file]
        args += ["--constraint", self.floors_file, *requirements]
        proc = _uv(args, self.timeout)
        return proc.returncode == 0, proc.stderr or proc.stdout

    def latest(self, names):
        """Latest version of each named package allowed in this column."""
        pins = {}
        # Looked up one at a time, since a name with no release would fail a batch.
        for name in names:
            pins.update(self.resolve([name], floors=False, no_deps=True)[0] or {})
        return {name: version for name, (version, _) in pins.items()}

    def write_floors(self, floors):
        # The public version drops any local segment (e.g. +g1a2b3c4 on a
        # nightly wheel), which uv does not accept in a '>=' constraint.
        lines = [f"{name}>={Version(v).public}" for name, v in sorted(floors.items())]
        Path(self.floors_file).write_text("\n".join(lines) + "\n")


def _requirements(pkg, variant, nightly):
    """Requirements that install one listed package, or None if there are none."""
    extras = pkg.get("install_extras") or []
    base = pkg["pypi_name"] + ("[" + ",".join(extras) + "]" if extras else "")
    extra_deps = pkg.get("extra_deps") or []
    if variant != "dev" or canonicalize_name(pkg["pypi_name"]) in nightly:
        return [base, *extra_deps]
    if pkg.get("repo_url"):
        return [f"{base} @ git+{pkg['repo_url']}", *extra_deps]
    return None


def _plan(column, requirements, listed, infra, log):
    """Work out which packages can share one venv without dragging dependencies down.

    ``requirements`` maps package name to its install requirements, and
    ``listed`` is the set of every listed package name. Returns
    {name: (install_status, error)} for packages that cannot be installed;
    any package not in it can be. Leaves the floors file in place for the
    installs that follow.
    """

    def resolve_all(names, floors):
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [(requirements[name] + infra, floors) for name in names]
            return dict(zip(names, pool.map(lambda job: column.resolve(*job), jobs)))

    failed = {}
    floors = {}
    for name, (pins, error) in resolve_all(list(requirements), False).items():
        if pins is None:
            failed[name] = (status.INSTALL_FAIL, error)
            log(f"  {name}: cannot be resolved")
            continue
        # Listed packages, the top-level requirements (which have no
        # requirers), and whatever a listed package requires directly.
        for dep, (version, via) in pins.items():
            if version and (dep in listed or not via or via & listed):
                if dep not in floors or Version(version) > Version(floors[dep]):
                    floors[dep] = version

    column.write_floors(floors)
    candidates = [name for name in requirements if name not in failed]
    for name, (pins, error) in resolve_all(candidates, True).items():
        if pins is None:
            failed[name] = (status.HELD_BACK, error)
            log(f"  {name}: held back")
    return failed


def run_variant(
    variant, python_version, packages, cfg, repo_root, results_dir, timeouts
):
    print(f"\n=== Variant: {variant} (Python {python_version}) ===")
    constraints = [c for c in cfg["constraints"] if variant in c["variants"]]
    result = {
        "variant": variant,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "finished_at": "",
        "python_requested": python_version,
        "python_version": "",
        "constraints": constraints,
        "timeout_test_seconds": timeouts["test"],
        "pytest_limit_n": (
            int(os.environ["PYTEST_LIMIT_N"])
            if (os.environ.get("PYTEST_LIMIT_N") or "").isdigit()
            else None
        ),
        "fatal_error": "",
        "installed_deps": {},
        "packages": [],
    }
    out_path = results_dir / f"{variant}__{python_version}.json"
    entries = {}

    Path(repo_root, ".tmp").mkdir(exist_ok=True)
    tmpdir = tempfile.mkdtemp(
        prefix=f"int-{variant}-{python_version}-",
        dir=str(Path(repo_root, ".tmp").resolve()),
    )

    try:
        venv = os.path.join(tmpdir, "venv")
        proc = _uv(
            ["venv", venv, "-p", ensure_python(python_version), "-q"], timeout=120
        )
        if proc.returncode != 0:
            result["fatal_error"] = f"uv venv: {proc.stderr or proc.stdout}"
            return result, out_path
        python = os.path.join(venv, "bin", "python")
        result["python_version"] = _venv_python_version(python)

        constraints_file = os.path.join(tmpdir, "constraints.txt")
        Path(constraints_file).write_text(
            "".join(c["spec"] + "\n" for c in constraints)
        )
        column = Column(
            variant,
            python,
            cfg["dev_index_urls"],
            constraints_file,
            tmpdir,
            timeouts["install"],
        )

        # In dev columns a listed package comes from a nightly wheel when one
        # of the dev indexes has one, and is built from git otherwise.
        nightly = set()
        if variant == "dev" and cfg["dev_index_urls"]:
            latest = column.latest(
                {canonicalize_name(p["pypi_name"]) for p in packages}
            )
            nightly = {n for n, v in latest.items() if v and Version(v).is_devrelease}

        requirements = {}
        for pkg in packages:
            name = canonicalize_name(pkg["pypi_name"])
            reqs = _requirements(pkg, variant, nightly)
            entries[name] = {
                "name": pkg["pypi_name"],
                "tier": pkg.get("tier", ""),
                "module": pkg["module"],
                "install_spec": " ".join(reqs or []),
                "resolved_version": "",
                "install_status": "",
                "install_error": "",
                "test_status": "",
                "test_output": "",
                "duration": 0,
            }
            if reqs is None:
                entries[name]["install_status"] = status.NO_SPEC
                entries[name]["install_error"] = (
                    "no nightly wheel and no repo_url to build from"
                )
            else:
                requirements[name] = reqs
            print(
                f"  {pkg['pypi_name']:<20} {entries[name]['install_spec'] or '(no install spec)'}"
            )

        infra = TEST_INFRA + cfg["test_deps"]
        print("\nPlanning...")
        listed = {canonicalize_name(p["pypi_name"]) for p in packages}
        for name, (install_status, error) in _plan(
            column, requirements, listed, infra, print
        ).items():
            entries[name]["install_status"] = install_status
            entries[name]["install_error"] = error

        print("\nInstalling pytest and test dependencies...")
        ok, error = column.install(infra)
        if not ok:
            print(error)
            result["fatal_error"] = error
            return result, out_path

        to_test = []
        for name, reqs in requirements.items():
            entry = entries[name]
            if entry["install_status"]:
                continue
            print(f"\nInstalling {entry['name']}...")
            ok, error = column.install(reqs)
            if ok:
                entry["install_status"] = status.INSTALLED
                to_test.append((name, entry))
            else:
                entry["install_status"] = status.INSTALL_FAIL
                entry["install_error"] = error
                print(f"  install failed\n{error}")

        result["installed_deps"] = _installed(python)
        for name, entry in to_test:
            entry["resolved_version"] = result["installed_deps"].get(name) or ""
            print(f"  {entry['name']} {entry['resolved_version']}")

        pytest_args = {
            canonicalize_name(p["pypi_name"]): p.get("pytest_args", [])
            for p in packages
        }
        for name, entry in to_test:
            print(f"\nTesting {entry['name']}...")
            entry["test_status"], entry["test_output"], entry["duration"] = run_tests(
                python, entry["module"], pytest_args[name], repo_root, timeouts["test"]
            )
            print(f"  {entry['test_status']} in {entry['duration']}s")

    finally:
        result["packages"] = list(entries.values())
        result["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out_path.write_text(json.dumps(result, indent=2))
        shutil.rmtree(tmpdir, ignore_errors=True)

    return result, out_path


def run_tests(
    python, module, pytest_args, repo_root, timeout, junit_path=None, traceback="line"
):
    """Run one package's tests with ``pytest --pyargs``.

    Runs from ``repo_root`` so the consuming repo's ``conftest.py`` applies.
    Returns (test_status, output, duration in seconds).
    """
    cmd = [python, "-m", "pytest", "--pyargs", module, *pytest_args]
    cmd += ["--timeout=120", "-q", f"--tb={traceback}", "--no-header"]
    if junit_path:
        cmd.append(f"--junitxml={junit_path}")
    env = {**os.environ, "MPLBACKEND": "Agg", "DISPLAY": ""}
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=repo_root, env=env
        )
    except subprocess.TimeoutExpired:
        return status.TIMEOUT, "timeout", round(time.time() - start, 1)
    duration = round(time.time() - start, 1)
    out = proc.stdout
    if proc.stderr:
        out += "\n--- stderr ---\n" + proc.stderr
    no_module = proc.returncode == 4 and "module or package not found" in out
    if proc.returncode == 5 or no_module:
        return status.NO_TESTS, out, duration
    return (status.PASS if proc.returncode == 0 else status.FAIL), out, duration


def _sort_by_tier(packages):
    """Group by tier in order of first appearance, alphabetical within each tier.

    Tiers carry no built-in ranking: whichever tier appears first in the
    config is displayed first. Packages without a tier share one group.
    """
    tier_rank = {}
    for pkg in packages:
        tier_rank.setdefault(pkg.get("tier", ""), len(tier_rank))
    return sorted(
        packages, key=lambda p: (tier_rank[p.get("tier", "")], p["pypi_name"].lower())
    )


def _counts(result):
    install = Counter(e["install_status"] for e in result["packages"])
    test = Counter(e["test_status"] for e in result["packages"] if e["test_status"])
    return {"install": dict(install), "test": dict(test)}


def add_arguments(ap):
    ap.add_argument("--config", default="packages.yaml")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument(
        "--variant",
        choices=status.VARIANTS,
        help="Only run configured columns with this variant; "
        "if omitted, every configured column runs.",
    )
    ap.add_argument(
        "--python",
        help="Only run configured columns with this Python version "
        "(e.g. '3.12', '3.14t'); if omitted, every configured column runs.",
    )
    ap.add_argument("--packages", help="Comma-separated subset of package names to run")
    ap.add_argument(
        "--tiers",
        help="Comma-separated subset of tiers to run (matching the 'tier' "
        "values in the config); default: all tiers",
    )
    ap.add_argument("--timeout-install", type=int, default=900)
    ap.add_argument("--timeout-test", type=int, default=1800)


def run(args):
    # Make every print flush immediately so CI streams progress live
    # instead of buffering until the script exits.
    sys.stdout.reconfigure(line_buffering=True)

    cfg = config.load_settings(args.config)
    packages = select_packages(
        config.load_packages(args.config), args.tiers, args.packages
    )
    packages = _sort_by_tier(packages)

    repo_root = Path.cwd()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    timeouts = {"install": args.timeout_install, "test": args.timeout_test}

    columns = config.load_columns(args.config)
    if args.variant:
        columns = [c for c in columns if c["variant"] == args.variant]
    if args.python:
        columns = [c for c in columns if c["python"] == args.python]
    if not columns:
        sys.exit("No configured columns match the given --variant/--python filters.")

    fatal = []
    for col in columns:
        result, out_path = run_variant(
            col["variant"],
            col["python"],
            packages,
            cfg,
            repo_root,
            results_dir,
            timeouts,
        )
        print(f"\nDone {col['variant']}/{col['python']}: {_counts(result)}")
        print(f"Wrote {out_path}")
        if result["fatal_error"]:
            fatal.append(f"{col['variant']}/{col['python']}")

    if fatal:
        sys.exit(f"\nCould not set up the test environment for: {', '.join(fatal)}")
