"""Load and validate the integration-testing config (``packages.yaml``).

Top-level keys:

  - ``columns``: a flat list of ``{python, variant}`` pairs. Each one is a
    single run and a single dashboard column.
  - ``packages``: the packages to install and test together (one entry =
    one dashboard row). Every listed package follows the column's variant.
  - ``dev_index_urls`` (optional): extra package indexes, such as nightly
    wheel channels, used by ``dev`` columns for any package they host.
  - ``test_deps`` (optional): pytest plugins installed alongside pytest in
    every column, for example one that registers a marker the packages'
    tests use.
  - ``constraints`` (optional): temporary limits on dependencies that are
    not themselves listed, each with a ``spec``, a ``reason`` and
    optionally the ``variants`` it applies to.
  - ``dashboard_title`` (optional): the dashboard heading.

Both ``run`` and ``dashboard`` read this file, so the parsing and
validation live here in one place.
"""

from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import status


def _load(path):
    data = yaml.safe_load(Path(path).read_text()) or {}
    if "core_package" in data:
        raise ValueError(
            f"{path}: 'core_package' is no longer supported; list that package "
            "under 'packages' and move its 'dev_index_urls' and 'test_deps' to "
            "the top level"
        )
    return data


def load_packages(path):
    packages = list(_load(path).get("packages") or [])
    for i, pkg in enumerate(packages):
        if not pkg.get("pypi_name"):
            raise ValueError(f"{path}: packages[{i}] needs a 'pypi_name'")
        pkg.setdefault("module", pkg["pypi_name"])
    return packages


def load_columns(path):
    """Return the validated ``columns`` list as ``[{python, variant}, ...]``.

    Order is preserved: it drives both the run order and the dashboard
    column order.
    """
    raw = _load(path).get("columns") or []
    columns = []
    for i, col in enumerate(raw):
        python = col.get("python")
        variant = col.get("variant")
        if not python or not variant:
            raise ValueError(f"{path}: columns[{i}] needs both 'python' and 'variant'")
        if variant not in status.VARIANTS:
            raise ValueError(
                f"{path}: columns[{i}] has variant '{variant}'; "
                f"expected one of {', '.join(status.VARIANTS)}"
            )
        columns.append({"python": str(python), "variant": variant})
    if not columns:
        raise ValueError(f"{path}: at least one entry under 'columns' is required")
    return columns


def load_constraints(path):
    """Return the validated ``constraints`` list.

    Each entry is ``{spec, reason, variants}``. A constraint may not name a
    listed package: listed packages always follow their column's variant.
    """
    listed = {canonicalize_name(p["pypi_name"]) for p in load_packages(path)}
    constraints = []
    for i, entry in enumerate(_load(path).get("constraints") or []):
        spec = entry.get("spec")
        if not spec or not entry.get("reason"):
            raise ValueError(f"{path}: constraints[{i}] needs a 'spec' and a 'reason'")
        try:
            name = canonicalize_name(Requirement(spec).name)
        except InvalidRequirement as exc:
            raise ValueError(f"{path}: constraints[{i}]: {exc}") from None
        if name in listed:
            raise ValueError(
                f"{path}: constraints[{i}] constrains '{name}', which is a listed "
                "package; constraints may only apply to unlisted dependencies"
            )
        variants = entry.get("variants") or list(status.VARIANTS)
        unknown = set(variants) - set(status.VARIANTS)
        if unknown:
            raise ValueError(
                f"{path}: constraints[{i}] has unknown variants {sorted(unknown)}"
            )
        constraints.append(
            {"spec": spec, "reason": entry["reason"], "variants": list(variants)}
        )
    return constraints


def load_settings(path):
    """The settings that apply to every install: constraints, dev indexes, test deps."""
    data = _load(path)
    return {
        "constraints": load_constraints(path),
        "dev_index_urls": list(data.get("dev_index_urls") or []),
        "test_deps": list(data.get("test_deps") or []),
    }


def load_dashboard_title(path):
    return _load(path).get("dashboard_title") or "Ecosystem integration matrix"
