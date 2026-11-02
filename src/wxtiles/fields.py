"""Field catalog: how each mapped field is found in a file, converted, quantized and colored.

Messages are identified by their GRIB2 numbers (discipline, category, parameter, level type,
level), never by position in the file and never by a center's short name, so the same rule finds
2 m temperature in an NCEP, ECMWF, DWD or MSC file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class GribId:
    discipline: int
    category: int
    number: int
    level_type: int
    level: int


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    units: str
    colormap: str
    quantum: float  # values are rounded to this before the COG is written, as GRIB packing does
    grib: tuple[GribId, ...] = ()
    idx: tuple[str, ...] = ()  # wgrib2 "VAR:level" strings
    ecmwf: tuple[str, ...] = ()  # ECMWF .index "param" values
    accumulated: bool = False
    derived_from: tuple[str, ...] = field(default_factory=tuple)


T2M = GribId(0, 0, 0, 103, 2)
PRMSL = GribId(0, 3, 1, 101, 0)
MSL_ECMWF = GribId(0, 3, 0, 101, 0)
MSLMA = GribId(0, 3, 198, 101, 0)
APCP = GribId(0, 1, 8, 1, 0)
TOT_PREC = GribId(0, 1, 52, 1, 0)
U10 = GribId(0, 2, 2, 103, 10)
V10 = GribId(0, 2, 3, 103, 10)
REFC = GribId(0, 16, 196, 10, 0)
MRMS_REFL = GribId(209, 10, 0, 102, 500)

FIELDS: dict[str, Field] = {
    "t2m": Field("t2m", "2 m temperature", "°F", "temperature", 0.1, (T2M,), ("TMP:2 m above ground",), ("2t",)),
    "mslp": Field("mslp", "Sea-level pressure", "hPa", "pressure", 0.1, (PRMSL, MSL_ECMWF, MSLMA),
                  ("PRMSL:mean sea level", "MSLMA:mean sea level"), ("msl",)),
    "qpf": Field("qpf", "Total precipitation", "in", "precip", 0.01, (APCP, TOT_PREC), ("APCP:surface",), ("tp",),
                 accumulated=True),
    "u10": Field("u10", "10 m u-wind", "m/s", "wind", 0.1, (U10,), ("UGRD:10 m above ground",), ("10u",)),
    "v10": Field("v10", "10 m v-wind", "m/s", "wind", 0.1, (V10,), ("VGRD:10 m above ground",), ("10v",)),
    "wind10": Field("wind10", "10 m wind speed", "mph", "wind", 0.1, derived_from=("u10", "v10")),
    "refc": Field("refc", "Composite reflectivity", "dBZ", "reflectivity", 0.5, (REFC,),
                  ("REFC:entire atmosphere",)),
    "refl": Field("refl", "Radar reflectivity (MRMS)", "dBZ", "reflectivity", 0.5, (MRMS_REFL,)),
    "ir": Field("ir", "Clean IR, band 13", "°C", "infrared", 0.1),
    "t2m_mean": Field("t2m_mean", "2 m temperature, ensemble mean", "°F", "temperature", 0.1),
    "t2m_spread": Field("t2m_spread", "2 m temperature, ensemble spread", "°F", "spread", 0.1),
    "qpf_mean": Field("qpf_mean", "Total precipitation, ensemble mean", "in", "precip", 0.01),
    "qpf_p1in": Field("qpf_p1in", "Chance of 1 inch or more", "%", "probability", 1.0),
}

# Which raw fields a mapped field needs from the file.
RAW_FOR = {
    "t2m": ["t2m"], "mslp": ["mslp"], "qpf": ["qpf"], "wind10": ["u10", "v10"], "refc": ["refc"],
    "refl": ["refl"], "ir": ["ir"],
}


def raw_fields(mapped: list[str]) -> list[str]:
    out: list[str] = []
    for m in mapped:
        for r in RAW_FOR[m]:
            if r not in out:
                out.append(r)
    return out


def identify(discipline: int, category: int, number: int, level_type: int, level: int) -> str | None:
    """Raw field name for a GRIB2 message, or None if it is not one we map."""
    gid = GribId(discipline, category, number, level_type, level)
    for name, f in FIELDS.items():
        if gid in f.grib:
            return name
    # Level is ignored for surface and column quantities, whose level value varies by center.
    for name, f in FIELDS.items():
        for g in f.grib:
            if (g.discipline, g.category, g.number, g.level_type) == (discipline, category, number, level_type) \
                    and level_type in (1, 10, 101, 102):
                return name
    return None


# ---------------------------------------------------------------------------- conversions
def k_to_f(a: np.ndarray) -> np.ndarray:
    return (a - 273.15) * 9.0 / 5.0 + 32.0


def to_display(name: str, a: np.ndarray, units: str = "") -> np.ndarray:
    """Convert a decoded array to the units the map shows."""
    if name in ("t2m",):
        return k_to_f(a)
    if name == "mslp":
        return a / 100.0
    if name == "qpf":
        return a / 25.4  # kg m-2 is mm of water
    if name == "ir":
        return a - 273.15
    return a


def wind_speed_mph(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return np.hypot(u, v) * 2.236936


def quantize(name: str, a: np.ndarray) -> np.ndarray:
    q = FIELDS[name].quantum
    out = np.round(a / q) * q
    return out.astype("float32")
