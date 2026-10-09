"""Repo-root pytest plugin for PR-preview smoke runs.

When the env var ``PYTEST_LIMIT_N`` is set to a positive integer, pytest
collects normally and then truncates the test list to the first N items.
The integration-dashboard runner sets this on pull requests so each
package surfaces a fast smoke signal (typically 10 tests) without any
per-package configuration.

This file must live at the root of the repository that invokes the runner
(this repo for its self-test; the consuming repo otherwise), because
``pytest --pyargs`` collects from site-packages and would otherwise never
load it. Downstream consumers copy this file into their repo root.
"""

import os


def pytest_collection_modifyitems(config, items):
    raw = os.environ.get("PYTEST_LIMIT_N")
    if not raw:
        return
    try:
        n = int(raw)
    except ValueError:
        return
    if n > 0 and len(items) > n:
        del items[n:]
