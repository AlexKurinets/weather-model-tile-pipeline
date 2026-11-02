import numpy as np

from wxtiles.colormaps import colorize
from wxtiles.qc import check_field, check_run_to_run
from wxtiles.registry import load_registry

LIMITS = load_registry().qc


def failed(checks):
    return {c.name for c in checks if not c.passed}


def test_good_temperature_passes():
    t = 280 + np.random.default_rng(1).normal(0, 8, (50, 60))
    assert failed(check_field("t2m", t, LIMITS)) == set()


def test_range_frozen_and_missing():
    assert "range" in failed(check_field("t2m", np.full((10, 10), 400.0) + np.arange(100).reshape(10, 10), LIMITS))
    assert "frozen" in failed(check_field("t2m", np.full((10, 10), 273.15), LIMITS))
    t = 280 + np.random.default_rng(2).normal(0, 5, (20, 20))
    t[:3] = np.nan
    assert "missing" in failed(check_field("t2m", t, LIMITS))


def test_dry_first_window_is_not_frozen_and_radar_gaps_are_allowed():
    assert failed(check_field("qpf", np.zeros((10, 10)), LIMITS)) == set()
    r = np.full((10, 10), np.nan)
    r[2:5, 2:5] = np.linspace(10, 50, 9).reshape(3, 3)
    assert failed(check_field("refl", r, LIMITS)) == set()


def test_run_to_run_jump():
    a = 50 + np.random.default_rng(3).normal(0, 10, (40, 40))
    assert check_run_to_run("t2m", a + 2, a, 21.6).passed
    assert not check_run_to_run("t2m", a + 40, a, 21.6).passed
    assert check_run_to_run("t2m", a, a[:20], 21.6).passed  # different grid: skipped, not failed


def test_colorize_is_transparent_where_masked_or_below_threshold():
    v = np.array([[0.0, 0.05, 1.0]])
    rgba = colorize(v, np.array([[False, False, True]]), "precip")
    assert rgba[0, 0, 3] == 0 and rgba[0, 1, 3] > 0 and rgba[0, 2, 3] == 0
    refl = colorize(np.array([[22.0, 24.9]]), np.zeros((1, 2), bool), "reflectivity")
    assert (refl[0, 0, :3] == refl[0, 1, :3]).all()  # stepped table: same 20-25 dBZ band
