"""Quality control on decoded fields, before anything is published.

Checks run on the values as the file carries them (K, Pa, kg m-2, m/s, dBZ), so limits do not
depend on display units. A failed check stops that step from publishing; the run it belongs to is
held and the previous complete run stays the default map.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from wxtiles.grids import matches_registry


@dataclass
class Check:
    field: str
    name: str
    passed: bool
    value: float | None
    detail: str


def check_field(name: str, values: np.ndarray, limits: dict) -> list[Check]:
    out: list[Check] = []
    finite = np.isfinite(values)
    if name != "refl":
        # MRMS marks everything outside radar coverage as missing, so its share varies with the
        # network and is not a fault; every other field should be complete.
        missing = 1.0 - float(finite.mean())
        max_missing = limits["max_missing_fraction"]
        out.append(Check(name, "missing", missing <= max_missing, round(missing, 4),
                         f"{missing:.1%} of points missing (limit {max_missing:.0%})"))
    if not finite.any():
        out.append(Check(name, "range", False, None, "no valid values"))
        return out
    v = values[finite]
    lo, hi = float(v.min()), float(v.max())
    lim = limits.get(name)
    if lim:
        ok = lo >= lim["min"] and hi <= lim["max"]
        out.append(Check(name, "range", ok, round(hi if hi > lim["max"] else lo, 3),
                         f"values {lo:.2f} to {hi:.2f}, allowed {lim['min']} to {lim['max']}"))
    std = float(v.std())
    frozen = std < limits["frozen_std"]
    if name == "qpf" and hi == 0.0:
        frozen = False  # a dry first window is legitimate
    out.append(Check(name, "frozen", not frozen, round(std, 5), f"standard deviation {std:.4f}"))
    return out


def check_grid(signature: dict, registry_grid: dict) -> Check:
    diffs = matches_registry(signature, registry_grid)
    return Check("grid", "grid", not diffs, None, "; ".join(diffs) if diffs else "matches registry")


def check_run_to_run(name: str, now: np.ndarray, prev: np.ndarray, limit: float) -> Check:
    """Mean absolute change against the previous run's forecast for the same valid time, on the
    same Mercator grid. A large jump is far more often a broken message than real weather."""
    if now.shape != prev.shape:
        return Check(name, "run_to_run", True, None, "previous run on a different grid, skipped")
    both = np.isfinite(now) & np.isfinite(prev)
    if both.mean() < 0.5:
        return Check(name, "run_to_run", True, None, "too little overlap, skipped")
    mad = float(np.abs(now[both] - prev[both]).mean())
    return Check(name, "run_to_run", mad <= limit, round(mad, 3),
                 f"mean absolute change {mad:.2f} against the previous run (limit {limit})")
