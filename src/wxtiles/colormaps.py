"""Color tables. Each is a list of (value, hex) stops in display units; values between stops are
interpolated, values below `clear_below` are transparent, and `steps` tables snap to the stop below
instead of blending (radar reflectivity is read in 5 dBZ bands)."""

from __future__ import annotations

import numpy as np

COLORMAPS: dict[str, dict] = {
    "temperature": {
        "stops": [(-40, "#e3d4f2"), (-20, "#9b6fc8"), (0, "#4b3fa8"), (15, "#2f6fd1"), (32, "#5ab4e5"),
                  (33, "#3d9a5d"), (45, "#8cc63f"), (60, "#f2e24b"), (75, "#f29b30"), (90, "#d63d27"),
                  (105, "#8a1538"), (120, "#f0c6d6")],
    },
    "pressure": {
        "stops": [(960, "#5b2a86"), (980, "#3f6fb5"), (995, "#7fb7d9"), (1005, "#e7eef2"), (1015, "#f2dfa7"),
                  (1025, "#e59a4d"), (1040, "#b0412e"), (1050, "#6d1d25")],
    },
    "precip": {
        "clear_below": 0.01,
        "stops": [(0.01, "#c7e9c0"), (0.1, "#74c476"), (0.25, "#2f9e44"), (0.5, "#1c6fb8"), (1.0, "#6a51a3"),
                  (1.5, "#c51b8a"), (2.0, "#e34a33"), (3.0, "#f0a030"), (5.0, "#fde0a0"), (10.0, "#ffffff")],
    },
    "wind": {
        "stops": [(0, "#1d2b53"), (10, "#2a6f97"), (20, "#4fb3a9"), (30, "#a7d65f"), (40, "#f2d04b"),
                  (50, "#f08c2e"), (65, "#d6402b"), (80, "#9c1f52"), (100, "#f2c6e4")],
    },
    "reflectivity": {
        "clear_below": 5.0,
        "steps": True,
        "stops": [(5, "#04e9e7"), (10, "#019ff4"), (15, "#0300f4"), (20, "#02fd02"), (25, "#01c501"),
                  (30, "#008e00"), (35, "#fdf802"), (40, "#e5bc00"), (45, "#fd9500"), (50, "#fd0000"),
                  (55, "#d40000"), (60, "#bc0000"), (65, "#f800fd"), (70, "#9854c6"), (75, "#fdfdfd")],
    },
    "infrared": {
        "stops": [(-90, "#ffffff"), (-80, "#e0e0e0"), (-70, "#e8361e"), (-60, "#f2c12e"), (-50, "#3fbf3f"),
                  (-40, "#2a6fdb"), (-30, "#8fd3f5"), (-20, "#d9d9d9"), (0, "#8c8c8c"), (30, "#262626"),
                  (50, "#000000")],
    },
    "spread": {
        "stops": [(0, "#f7f7f7"), (2, "#d1e5f0"), (4, "#92c5de"), (6, "#4393c3"), (9, "#2166ac"),
                  (12, "#053061")],
    },
    "probability": {
        "clear_below": 5.0,
        "stops": [(5, "#e5f5e0"), (20, "#a1d99b"), (40, "#41ab5d"), (60, "#fdae61"), (80, "#d7301f"),
                  (100, "#67000d")],
    },
}


def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def colorize(values: np.ndarray, mask: np.ndarray, name: str, alpha: int = 255) -> np.ndarray:
    """Values (display units) -> HxWx4 uint8 RGBA. `mask` is True where there is no data."""
    cm = COLORMAPS[name]
    xs = np.array([s[0] for s in cm["stops"]], dtype="float64")
    cols = np.array([_rgb(s[1]) for s in cm["stops"]], dtype="float64")
    v = np.nan_to_num(values.astype("float64"), nan=xs[0])
    out = np.zeros(values.shape + (4,), dtype="uint8")
    if cm.get("steps"):
        i = np.clip(np.searchsorted(xs, v, side="right") - 1, 0, len(xs) - 1)
        for c in range(3):
            out[..., c] = cols[i, c]
    else:
        for c in range(3):
            out[..., c] = np.interp(v, xs, cols[:, c]).round().astype("uint8")
    a = np.full(values.shape, alpha, dtype="uint8")
    a[mask | ~np.isfinite(values)] = 0
    if "clear_below" in cm:
        a[v < cm["clear_below"]] = 0
    out[..., 3] = a
    return out


def legend(name: str) -> dict:
    cm = COLORMAPS[name]
    return {"stops": [[v, c] for v, c in cm["stops"]], "steps": bool(cm.get("steps")),
            "clear_below": cm.get("clear_below")}
