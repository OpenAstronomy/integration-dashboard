integration-dashboard
======================

A small, ecosystem-agnostic harness for running cross-package integration
tests and publishing the results as a single-page dashboard. You point it
at a config file describing an ecosystem's core package and the packages
that depend on it, and it installs everything together into a shared
virtual environment, runs each package's test suite, and renders an
HTML matrix of the outcomes.

The goal is to catch problems that only appear when many packages from an
ecosystem are installed together, which each package's own CI cannot see.

This package is configuration-driven and contains nothing specific to any
one ecosystem. For a real-world configuration, see
[astropy/astropy-integration-testing](https://github.com/astropy/astropy-integration-testing),
which uses this package to test the Astropy ecosystem.

Installing
----------

```bash
pip install integration-dashboard
```

[uv](https://docs.astral.sh/uv/) is required at runtime: the harness shells
out to `uv` to create the venv and resolve/install packages.

How it works
------------

A run is organised as a matrix of *columns*. Each column is one
independent `(python, variant)` pair (the two axes are not a cross-product,
so you can test e.g. `3.12 + stable`, `3.13 + pre` and `3.14 + dev`). The
three variants control how the latest version of each package is chosen:

| Variant  | Core package                                          | Each package                           |
|----------|-------------------------------------------------------|----------------------------------------|
| `stable` | Latest non-pre-release on PyPI                        | Latest non-pre-release on PyPI         |
| `pre`    | Latest including pre-releases (`--prerelease=allow`)  | Latest including pre-releases          |
| `dev`    | Latest dev wheel from `core_package.dev_index_urls` (or `git+repo_url` if unset) | `git+<repo_url>` (HEAD of the default branch) |

Within each column a single shared venv is built: the core package is
installed first, then each package one at a time. If a package cannot be
installed alongside the existing venv (for example it pins an older version
of something already installed), it is skipped and recorded; the rest of
the venv is untouched. After the installs, `pytest --pyargs <module>` runs
for each package that installed successfully. Each column writes
`results/<variant>__<python>.json`, and the dashboard step reads those JSON
files and renders `site/index.html`.

Usage
-----

```bash
# Run every configured column. Each column can take 30-90 min depending on
# how many packages are configured.
integration-dashboard run --config packages.yaml

# Narrow to a single variant, a single column, or a single package:
integration-dashboard run --variant stable
integration-dashboard run --variant stable --python 3.12
integration-dashboard run --variant stable --packages foo,bar

# Restrict to a subset of tiers (default: all tiers):
integration-dashboard run --tiers core

# Build the dashboard from whatever results/*.json files exist:
integration-dashboard dashboard
```

`run` writes `results/<variant>__<python>.json`; `dashboard` writes
`site/index.html`. Preview it locally with
`python -m http.server -d site 8000`.

Configuration
-------------

Everything ecosystem-specific lives in a YAML config (conventionally
`packages.yaml`) with four top-level keys.

`core_package` is the package installed into the shared venv before
everything else:

```yaml
core_package:
  pypi_name: mycore
  module: mycore                # defaults to pypi_name
  repo_url: https://github.com/example/mycore.git   # used by the dev variant
  # dev variant: install nightly wheels from these indexes; omit to install
  # the dev version from git+repo_url instead.
  dev_index_urls:
    - https://pypi.example.org/mycore-nightly/simple
  # Optional pytest plugins shared across the ecosystem, installed into the
  # venv alongside pytest (e.g. a plugin that registers a marker so the
  # matching tests are skipped rather than run).
  test_deps:
    - pytest-someplugin
```

`columns` is the list of `(python, variant)` pairs to test; each becomes one
dashboard column. Python versions use uv notation, so `"3.14t"` is the
free-threaded 3.14 build. The dashboard groups consecutive columns that
share a Python version under a spanning header:

```yaml
columns:
  - {python: "3.12", variant: stable}
  - {python: "3.13", variant: pre}
  - {python: "3.14t", variant: dev}
```

`packages` is the per-package list (one entry = one dashboard row). Each
entry takes:

- `pypi_name` (the package's name on PyPI; also the row label)
- `module` (the top-level import name, for `pytest --pyargs`; defaults to `pypi_name`)
- `repo_url` (for the `dev` variant install)
- `tier` (optional label used for grouping/ordering and the `--tiers` filter;
  tiers are ordered by first appearance in the list, and entries with no
  tier share a single unlabelled group)
- `install_extras` (optional list, e.g. `[test, all]`)
- `extra_deps` (optional list of extra packages to add to the install)
- `pytest_args` (optional list passed through to pytest; use `-k "not foo"` to skip tests)

`dashboard_title` (optional) sets the dashboard heading.

To retarget the harness at a different ecosystem, point `core_package` at
that ecosystem's core package and replace the `packages` list.

PR-preview smoke runs
---------------------

`run` honours a `PYTEST_LIMIT_N` environment variable: when set, the result
JSON records the limit and the dashboard shows a banner, and the limit is
propagated to the test subprocesses. Truncating each package's collected
test list to the first N tests requires a repo-root `conftest.py` in the
project that invokes the harness (because `pytest --pyargs` collects from
site-packages); that file lives in the consuming repository, not here.
