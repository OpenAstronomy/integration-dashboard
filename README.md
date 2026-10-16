integration-dashboard
======================

A small, ecosystem-agnostic harness for running cross-package integration
tests and publishing the results as a single-page dashboard. You point it
at a config file listing an ecosystem's packages, and it installs them
together into a shared virtual environment, runs each package's test
suite, and renders an HTML matrix of the outcomes.

The goal is to catch problems that only appear when many packages from an
ecosystem are installed together, which each package's own CI cannot see.

This package is configuration-driven and contains nothing specific to any
one ecosystem. For a real-world configuration, see
[astropy/astropy-integration-testing](https://github.com/astropy/astropy-integration-testing),
which uses this package to test the Astropy ecosystem.

Installing
----------

The package is not yet released on PyPI, so install it from this repo:

```bash
pip install git+https://github.com/OpenAstronomy/integration-dashboard
```

[uv](https://docs.astral.sh/uv/) is required at runtime: the harness shells
out to `uv` to create the venv and resolve/install packages.

How it works
------------

A run is organised as a matrix of *columns*. Each column is one
independent `(python, variant)` pair (the two axes are not a cross-product,
so you can test e.g. `3.12 + stable`, `3.13 + pre` and `3.14 + dev`). The
variant applies to every listed package:

| Variant  | Each listed package                                                        |
|----------|----------------------------------------------------------------------------|
| `stable` | Latest release                                                             |
| `pre`    | Latest release or pre-release                                              |
| `dev`    | Nightly wheel from `dev_index_urls` if one exists, otherwise `git+<repo_url>` |

In `pre` and `dev` columns, dependencies may also come from pre-releases, and
in `dev` columns from the `dev_index_urls` too.

Within each column all listed packages share one venv, and no package may
drag a shared dependency down for the others. Each listed package is first
resolved on its own. Then, for every listed package and every direct
dependency of a listed package, the highest version any of those
resolutions reached becomes a minimum, and each package is resolved again
with those minimums. A package that only works with an older version of
something another package uses is reported as *held back*, with uv's
explanation of which dependency is involved, and is not installed or
tested. Dependencies further down the tree are left to uv, since their caps
come from packages outside the list. The remaining packages are installed,
and `pytest --pyargs <module>` runs for each one. Each column writes
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
integration-dashboard run --tiers coordinated

# Build the dashboard from whatever results/*.json files exist:
integration-dashboard dashboard
```

`run` writes `results/<variant>__<python>.json`; `dashboard` writes
`site/index.html`. Preview it locally with
`python -m http.server -d site 8000`.

Configuration
-------------

Everything ecosystem-specific lives in a YAML config (conventionally
`packages.yaml`).

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
- `repo_url` (built from in `dev` columns when no nightly wheel exists)
- `tier` (optional label used for grouping/ordering and the `--tiers` filter;
  tiers are ordered by first appearance in the list, and entries with no
  tier share a single unlabelled group)
- `install_extras` (optional list, e.g. `[test, all]`)
- `extra_deps` (optional list of extra packages to add to the install)
- `pytest_args` (optional list passed through to pytest; use `-k "not foo"` to skip tests)

`dev_index_urls` (optional) lists extra package indexes, such as nightly
wheel channels, that `dev` columns use for any package they host:

```yaml
dev_index_urls:
  - https://pypi.example.org/nightly/simple
```

`test_deps` (optional) lists pytest plugins installed alongside pytest in
every column, for example a plugin that registers a marker so the matching
tests are skipped rather than run:

```yaml
test_deps:
  - pytest-someplugin
```

`constraints` (optional) temporarily holds back dependencies that are not
themselves listed, for example when their latest release is broken or two
of them cannot coexist. Each entry needs a `spec` and a `reason`, and can be
limited to some `variants`. Constraints may not name a listed package, and
the dashboard shows them in a banner:

```yaml
constraints:
  - spec: docutils<0.23
    reason: sphinx does not support docutils 0.23 yet
  - spec: numpy!=2.5.0; python_version >= "3.14"
    reason: numpy 2.5.0 has no wheels for Python 3.14
    variants: [stable, pre]
```

`dashboard_title` (optional) sets the dashboard heading.

Comparing before and after a change
-----------------------------------

`integration-dashboard compare` answers a narrower question than the
dashboard: did a change to one package, typically a pull request, break any
of the packages that depend on it? It reuses the same config, but tests each
downstream package on its own rather than in a shared venv:

```bash
integration-dashboard compare --package astropy \
    --before git+https://github.com/astropy/astropy@<base commit> \
    --after /path/to/astropy-checkout \
    --tiers coordinated
```

`--before` and `--after` are pip URLs or local paths for the changed package.
A local path is built into a wheel once at the start of the run, so every
downstream package is tested against the same build of its current contents.
For each downstream package, the "before" venv is resolved and locked (git
installs are locked to a commit, so both runs test the same downstream code),
and the "after" venv is built from the same lock with only the changed package
swapped. The individual test results are then compared, so the output lists
the tests that newly fail even when a downstream package already has failing
tests of its own. The output shows the full traceback of every newly failing
test (and any install errors), then a one-line summary per package. The
command exits non-zero if anything newly fails, or if either environment fails
to install, since the change could then not be checked.

The `compare.yml` reusable workflow runs one job per downstream package, which
fails if that package has new failures; its log ends with the tracebacks and
the summary line. For example, in the changed package's repository:

```yaml
name: downstream
on:
  pull_request:
    types: [labeled, synchronize]
jobs:
  compare:
    if: contains(github.event.pull_request.labels.*.name, 'needs-downstream-testing')
    uses: OpenAstronomy/integration-dashboard/.github/workflows/compare.yml@main
    with:
      config-repository: astropy/astropy-integration-testing
      package: astropy
      # The PR's test merge commit is its base commit plus the PR's changes.
      before: git+https://github.com/astropy/astropy@${{ github.event.pull_request.base.sha }}
      after: git+https://github.com/astropy/astropy@${{ github.event.pull_request.merge_commit_sha }}
      tiers: coordinated
```

Continuous integration (reusable workflow)
------------------------------------------

This repo ships a [reusable workflow](https://docs.github.com/actions/using-workflows/reusing-workflows)
at `.github/workflows/integration.yml`, so a consuming repo only needs a
small caller workflow plus its own config. The caller workflow is the
~15 lines below; commit it alongside a `packages.yaml` and a `conftest.py`
(copy the one in this repo), and that is the entire setup:

```yaml
# .github/workflows/integration.yml in the consuming repo
name: integration
on:
  schedule:
    - cron: '0 6 * * 0'   # Sundays at 06:00 UTC
  workflow_dispatch:
  pull_request:

jobs:
  integration:
    permissions:
      contents: write     # to publish the dashboard to gh-pages
    uses: OpenAstronomy/integration-dashboard/.github/workflows/integration.yml@main
    with:
      config: packages.yaml
```

While the package is in early development there are no releases, so use
`@main` as above. By default the workflow also installs the package from
the main branch of this repo, so the workflow and the package stay in step.
To pin both to a specific commit, use that commit for the `uses:` ref and
set the `package-spec` input to the matching
`git+https://github.com/OpenAstronomy/integration-dashboard@<commit>`.
The reusable workflow reads `columns:` from your config to build the
matrix, runs every column, and on non-PR runs publishes the dashboard to
`gh-pages` (set the `publish: false` input to skip that).

If some packages need system libraries or tools to build or run their
tests, list them in the `libraries` input and they are installed before
each column runs. It takes the same format as the OpenAstronomy tox
workflow: a mapping from package manager (`apt`, `brew`, `brew-cask` or
`choco`) to a list of packages. Columns currently all run on Ubuntu, so
only the `apt` list is used for now:

```yaml
    with:
      config: packages.yaml
      libraries: |
        apt:
          - libfftw3-dev
          - ffmpeg
```

### PR previews

On pull requests the workflow uploads the rendered dashboard as an
artifact instead of publishing. To get a clickable "View dashboard
preview" check on the PR, also copy `.github/workflows/preview-link.yml`
from this repo into the consuming repo and change its `workflows:` entry
to the name of your caller workflow (`integration` above). It must be on
the default branch to take effect.

PRs also cap each package to the first few tests via the `PYTEST_LIMIT_N`
environment variable (the `pytest-limit-n` input): the runner records the
limit, propagates it to the test subprocesses, and the dashboard shows a
banner. The actual truncation is done by the repo-root `conftest.py`
(because `pytest --pyargs` collects from site-packages, the file must live
in the repo that invokes the harness), so copy that file into the
consuming repo as well.

### Dogfooding

This repo tests the package and the reusable workflow against itself:
`.github/workflows/self-test.yml` calls the same reusable workflow with
`package-spec: .` (the checked-out source rather than the main branch)
and the small `example/packages.yaml`, so every push and PR here exercises
the whole pipeline.
