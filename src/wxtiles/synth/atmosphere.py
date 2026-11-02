"""A synthetic atmosphere that every synthetic feed samples.

One deterministic "truth" (pressure systems that drift east and deepen and fill, a cold front
with convection, a land-sea diurnal cycle) is sampled on each feed's own grid. A model sees it
through a View: its systems run slightly fast or slow with lead time, it carries a temperature
bias and smooth noise that grows with lead, and the AI models' precipitation is weaker and
smoother. Ensemble members also displace each system by an amount that grows with lead. Analyses
and observations see the truth itself. The result is weather that looks coherent on a map, models
that disagree in plausible ways, and nothing that comes from a real forecast.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import numpy as np

from wxtiles.settings import settings

T_REF = datetime(2026, 10, 25, tzinfo=UTC)
R_EARTH_KM = 6371.0
G = 9.81


@dataclass(frozen=True)
class System:
    lon0: float  # position at T_REF
    lat0: float
    u: float  # degrees per hour
    v: float
    amp: float  # hPa at peak, negative for a low
    radius_km: float
    t_start: float  # hours after T_REF when it starts to develop
    life: float  # hours from start to fully filled


@dataclass(frozen=True)
class Cell:
    along: float  # position along the cold front, 0 at the low, 1 at the far end
    across_km: float
    t_birth: float
    life: float
    peak: float  # mm/h
    radius_km: float


@dataclass(frozen=True)
class View:
    lead_h: float = 0.0
    speed: float = 1.0
    amp: float = 1.0
    t_bias: float = 0.0
    noise_k: float = 0.0
    noise_seed: int = 0
    precip_scale: float = 1.0
    smooth: float = 0.0
    member: int | None = None
    member_spread: float = 0.0


def _systems() -> list[System]:
    # The North American systems are placed by hand so the demo day has a mature Great Lakes low
    # with a trailing cold front, a Great Basin high and a second low developing in the Plains.
    s = [
        System(-133.4, 39.4, 0.45, 0.05, -24.0, 900.0, -40.0, 260.0),
        System(-133.4, 34.0, 0.30, 0.01, 16.0, 1300.0, -60.0, 400.0),
        System(-140.0, 36.0, 0.42, 0.04, -18.0, 750.0, 100.0, 200.0),
        System(-89.0, 15.0, -0.10, 0.11, -30.0, 260.0, 60.0, 160.0),
        System(-60.0, 36.0, 0.25, 0.02, 14.0, 1500.0, -100.0, 500.0),
    ]
    rng = np.random.default_rng(7)
    for _ in range(14):
        hemi = rng.choice([-1.0, 1.0])
        low = rng.random() < 0.6
        lat = hemi * (rng.uniform(40, 62) if low else rng.uniform(25, 40))
        s.append(System(rng.uniform(-180, 180), lat, rng.uniform(0.25, 0.55), hemi * rng.uniform(0.0, 0.05),
                        (-1 if low else 1) * rng.uniform(10, 28 if low else 18), rng.uniform(700, 1400),
                        rng.uniform(-150, 150), rng.uniform(200, 420)))
    return s


SYSTEMS = _systems()


def _cells() -> list[Cell]:
    rng = np.random.default_rng(11)
    return [Cell(rng.uniform(0.05, 1.0), rng.normal(0, 60), rng.uniform(-20, 400), rng.uniform(3, 7),
                 rng.uniform(15, 70), rng.uniform(18, 40)) for _ in range(900)]


CELLS = _cells()


def hours(t: datetime) -> float:
    return (t - T_REF).total_seconds() / 3600.0


def _envelope(sys: System, th: float) -> float:
    x = (th - sys.t_start) / sys.life
    return float(math.sin(math.pi * x)) if 0.0 < x < 1.0 else 0.0


def _member_offsets(member: int | None, n: int) -> np.ndarray:
    if member is None or member == 0:
        return np.zeros((n, 3))
    return np.random.default_rng(1000 + member).normal(0, 1, (n, 3))


KM_PER_DEG = math.pi / 180.0 * R_EARTH_KM


def _wrap(dlon: np.ndarray) -> np.ndarray:
    return np.where(dlon > 180.0, dlon - 360.0, np.where(dlon < -180.0, dlon + 360.0, dlon))


def _local_km(lon: np.ndarray, lat: np.ndarray, lon_c: float, lat_c: float,
              coslat: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    c = np.cos(np.radians(lat)) if coslat is None else coslat
    return _wrap(lon - lon_c) * KM_PER_DEG * c, (lat - lat_c) * KM_PER_DEG


def _band(lat: np.ndarray, lat_c: float, reach_km: float) -> np.ndarray | None:
    """Points within reach of a feature in latitude (a cheap pre-filter), or None if there are none."""
    m = np.abs(lat - lat_c) < reach_km / KM_PER_DEG
    return m if m.any() else None


def _positions(th_eff: float, view: View) -> list[tuple[System, float, float, float]]:
    offs = _member_offsets(view.member, len(SYSTEMS))
    out = []
    for i, s in enumerate(SYSTEMS):
        env = _envelope(s, th_eff)
        if env <= 0.0:
            continue
        grow = view.member_spread * view.lead_h * 0.3
        lon = ((s.lon0 + s.u * th_eff + offs[i, 0] * grow + 180.0) % 360.0) - 180.0
        lat = float(np.clip(s.lat0 + s.v * th_eff + offs[i, 1] * grow * 0.6, -80, 80))
        amp = s.amp * env * view.amp * (1.0 + 0.08 * offs[i, 2] * view.lead_h / 120.0)
        out.append((s, lon, lat, amp))
    return out


@lru_cache(maxsize=1)
def _land_grid() -> np.ndarray:
    """Land fraction on a 0.5 degree grid from Natural Earth 1:110m land, lightly blurred."""
    from rasterio import features
    from rasterio.transform import from_origin

    path = Path(settings.web_dir) / "geo" / "ne_110m_land.geojson"
    shapes = [f["geometry"] for f in json.loads(path.read_text(encoding="utf-8"))["features"]]
    tr = from_origin(-180.0, 90.0, 0.5, 0.5)
    m = features.rasterize(((g, 1) for g in shapes), out_shape=(360, 720), transform=tr, fill=0, dtype="uint8")
    m = m.astype("float32")
    k = np.ones(3) / 3.0
    m = np.apply_along_axis(lambda r: np.convolve(np.r_[r[-1], r, r[0]], k, mode="valid"), 1, m)
    m = np.apply_along_axis(lambda c: np.convolve(np.r_[c[0], c, c[-1]], k, mode="valid"), 0, m)
    return m


def land_fraction(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    g = _land_grid()
    x = (_wrap(lon) + 180.0) / 0.5 - 0.5
    y = (90.0 - np.clip(lat, -89.75, 89.75)) / 0.5 - 0.5
    x0 = np.floor(x).astype(int)
    y0 = np.clip(np.floor(y).astype(int), 0, 358)
    fx, fy = x - x0, np.clip(y - y0, 0, 1)
    x0 %= 720
    x1 = (x0 + 1) % 720
    return ((g[y0, x0] * (1 - fx) + g[y0, x1] * fx) * (1 - fy) + (g[y0 + 1, x0] * (1 - fx) + g[y0 + 1, x1] * fx) * fy)


def smooth_noise(lon: np.ndarray, lat: np.ndarray, seed: int, n: int = 7) -> np.ndarray:
    """Deterministic smooth noise, unit standard deviation, wavelengths of 1,500 to 6,000 km."""
    rng = np.random.default_rng(seed)
    out = np.zeros_like(lon, dtype="float64")
    rlon, rlat = np.radians(lon), np.radians(lat)
    for _ in range(n):
        k = rng.uniform(3, 12)
        ang = rng.uniform(0, 2 * math.pi)
        out += np.sin(k * (math.cos(ang) * rlon * np.cos(rlat) + math.sin(ang) * rlat) + rng.uniform(0, 2 * math.pi))
    return out / math.sqrt(n / 2.0)


def texture(lon: np.ndarray, lat: np.ndarray, seed: int, wavelength_deg: float, n: int = 8) -> np.ndarray:
    """Isotropic small-scale texture, unit standard deviation, wavelengths around `wavelength_deg`
    measured along the ground (longitude is shrunk by cos(latitude) first)."""
    rng = np.random.default_rng(seed)
    x = lon * np.cos(np.radians(lat))
    out = np.zeros_like(x, dtype="float64")
    for _ in range(n):
        k = 2 * math.pi / (wavelength_deg * rng.uniform(0.6, 1.6))
        ang = rng.uniform(0, math.pi)
        out += np.sin(k * (math.cos(ang) * x + math.sin(ang) * lat) + rng.uniform(0, 2 * math.pi))
    return out / math.sqrt(n / 2.0)


def _pressure(lon, lat, coslat, pos, view):
    p = np.full(lon.shape, 1013.0)
    dpx = np.zeros(lon.shape)
    dpy = np.zeros(lon.shape)
    widen = 1.0 + 0.06 * view.smooth
    for s, slon, slat, amp in pos:
        sig = s.radius_km * widen
        m = _band(lat, slat, 4.0 * sig)
        if m is None:
            continue
        x, y = _local_km(lon[m], lat[m], slon, slat, coslat[m])
        g = amp * np.exp(-(x * x + y * y) / (2 * sig * sig))
        p[m] += g
        dpx[m] += -g * x / (sig * sig)
        dpy[m] += -g * y / (sig * sig)
    return p, dpx, dpy  # hPa, hPa per km


def _wind(lat, dpx, dpy):
    """Geostrophic wind from the pressure gradient, slowed and turned towards low pressure."""
    f = 2 * 7.292e-5 * np.sin(np.radians(np.where(np.abs(lat) < 12, np.sign(lat + 1e-9) * 12, lat)))
    rho = 1.2
    gx, gy = dpx * 100.0 / 1000.0, dpy * 100.0 / 1000.0  # Pa per m
    ug, vg = -gy / (rho * f), gx / (rho * f)
    a = np.radians(25.0) * np.sign(lat + 1e-9)
    u = 0.55 * (ug * np.cos(a) - vg * np.sin(a))
    v = 0.55 * (ug * np.sin(a) + vg * np.cos(a))
    spd = np.hypot(u, v)
    cap = np.where(spd > 45.0, 45.0 / np.maximum(spd, 1e-6), 1.0)
    return u * cap, v * cap


def _rate(lon, lat, coslat, th_eff, pos, view):
    """Precipitation rate, mm/h: a shield north-east of each low, a band along its cold front,
    and convective cells that live a few hours on the front of the main low."""
    r = np.zeros(lon.shape)
    widen = 1.0 + 0.1 * view.smooth
    for s, slon, slat, amp in pos:
        if amp >= 0:
            continue
        strength = -amp / 25.0
        m = _band(lat, slat, 3.2 * s.radius_km * widen)
        if m is None:
            continue
        x, y = _local_km(lon[m], lat[m], slon, slat, coslat[m])
        sig = s.radius_km * 0.45 * widen
        rr = 3.0 * strength * np.exp(-(((x - 0.35 * s.radius_km) ** 2) + (y - 0.25 * s.radius_km) ** 2)
                                     / (2 * sig * sig))
        # Cold front trailing south-west: distance from the line through the low at 235 degrees.
        ang = math.radians(235.0 if slat >= 0 else 125.0)
        dx, dy = math.cos(ang), math.sin(ang)
        along = x * dx + y * dy
        across = -x * dy + y * dx
        length = 2.2 * s.radius_km
        band = np.exp(-(across ** 2) / (2 * (90.0 * widen) ** 2)) * np.clip(along / (0.2 * length), 0, 1) \
            * np.exp(-np.clip(along - length, 0, None) ** 2 / (2 * (0.3 * length) ** 2))
        r[m] += rr + 2.2 * strength * band
    main = [p for p in pos if p[0] is SYSTEMS[0]]
    if main:
        s, slon, slat, amp = main[0]
        ang = math.radians(235.0)
        length = 2.2 * s.radius_km
        for c in CELLS:
            age = th_eff - c.t_birth
            if not 0.0 < age < c.life:
                continue
            env = math.sin(math.pi * age / c.life)
            cx_km = math.cos(ang) * c.along * length - math.sin(ang) * c.across_km
            cy_km = math.sin(ang) * c.along * length + math.cos(ang) * c.across_km
            clat = slat + cy_km / (math.pi / 180.0 * R_EARTH_KM)
            clon = slon + cx_km / (math.pi / 180.0 * R_EARTH_KM * math.cos(math.radians(clat)))
            rad = c.radius_km * widen
            m = _band(lat, clat, 4.0 * rad)
            if m is None:
                continue
            x, y = _local_km(lon[m], lat[m], clon, clat, coslat[m])
            peak = c.peak * env / (1.0 + 0.6 * view.smooth)
            r[m] += peak * np.exp(-(x * x + y * y) / (2 * rad * rad))
    # Break the smooth shapes into bands and cells with texture that drifts east with the weather.
    shift = lon - 0.42 * th_eff
    tex = 0.55 + 0.45 * texture(shift, lat, 23, 3.5) + 0.25 * texture(shift, lat, 29, 1.2)
    return r * np.clip(tex, 0.03, 2.0) * view.precip_scale


def sample(valid: datetime, lon: np.ndarray, lat: np.ndarray, view: View = View(), wanted=("t2m",)) -> dict:
    """Fields at `valid` on points lon/lat (degrees). Units: t2m K, mslp Pa, u10/v10 m/s,
    rate mm/h, refc/refl dBZ, ir K."""
    lon = np.asarray(lon, "float64")
    lat = np.asarray(lat, "float64")
    th = hours(valid)
    th_eff = th - view.lead_h + view.speed * view.lead_h
    pos = _positions(th_eff, view)
    coslat = np.cos(np.radians(lat))
    out = {}
    need_p = any(w in wanted for w in ("mslp", "u10", "v10", "t2m"))
    if need_p:
        p, dpx, dpy = _pressure(lon, lat, coslat, pos, view)
    if "mslp" in wanted:
        n = smooth_noise(lon, lat, view.noise_seed + 1) * view.noise_k * 1.6 if view.noise_k else 0.0
        out["mslp"] = (p + n) * 100.0
    if "u10" in wanted or "v10" in wanted:
        u, v = _wind(lat, dpx, dpy)
        out["u10"], out["v10"] = u, v
    if "t2m" in wanted or "ir" in wanted:
        land = land_fraction(lon, lat)
        slat = np.sin(np.radians(lat))
        base = 300.5 - 47.0 * slat ** 2 - 6.0 * slat
        lst = (valid.hour + valid.minute / 60.0 + lon / 15.0) % 24.0
        diurnal = (1.0 + 6.0 * land) * np.cos(2 * math.pi * (lst - 15.0) / 24.0)
        adv = np.zeros(lon.shape)
        for s, slon, slat_c, amp in pos:
            if amp >= 0:
                continue
            sig = s.radius_km * 1.1
            m = _band(lat, slat_c, 4.0 * sig)
            if m is None:
                continue
            x, y = _local_km(lon[m], lat[m], slon, slat_c, coslat[m])
            adv[m] += (-amp / 25.0) * 7.0 * (x / sig) * np.exp(-(x * x + y * y) / (2 * sig * sig))
        t = base + diurnal + adv - 3.0 * land * np.clip(np.abs(lat) - 30, 0, None) / 30.0 + view.t_bias
        if view.noise_k:
            t = t + smooth_noise(lon, lat, view.noise_seed) * view.noise_k
        out["t2m"] = t
    need_rate = any(w in wanted for w in ("rate", "refc", "refl", "ir"))
    if need_rate:
        rate = _rate(lon, lat, coslat, th_eff, pos, view)
        out["rate"] = rate
        dbz = 23.0 + 16.0 * np.log10(np.maximum(rate, 1e-3))
        if "refc" in wanted or "refl" in wanted:
            out["refc"] = np.clip(dbz, -30.0, 75.0)
        if "ir" in wanted:
            cloud = 1.0 - np.exp(-rate / 0.4)
            top = np.where(rate > 8.0, 205.0, 232.0 - 2.0 * np.log1p(rate))
            shift = lon - 0.42 * th_eff
            tops = texture(shift, lat, 99, 2.0) * 3.0
            cirrus = 0.35 * np.clip(texture(shift, lat, 5, 9.0), 0, None)
            cf = np.clip(cloud + cirrus * (1 - cloud), 0, 1)
            out["ir"] = (1 - cf) * (out["t2m"] - 2.0) + cf * (top + tops)
    return out


def accumulate(t1: datetime, t2: datetime, lon: np.ndarray, lat: np.ndarray, view: View, cycle: datetime,
               dt_h: int = 1) -> np.ndarray:
    """Precipitation (mm = kg m-2) between t1 and t2 for a model run from `cycle`: rates sampled
    at the middle of each dt_h-hour block, times dt_h."""
    total = np.zeros(np.shape(lon))
    hours_ = int(round((t2 - t1).total_seconds() / 3600.0))
    dt_h = dt_h if hours_ % dt_h == 0 else 1
    for i in range(hours_ // dt_h):
        tm = t1.timestamp() + (i + 0.5) * dt_h * 3600.0
        valid = datetime.fromtimestamp(tm, UTC)
        lead = (valid - cycle).total_seconds() / 3600.0
        v = View(lead, view.speed, view.amp, view.t_bias, view.noise_k, view.noise_seed, view.precip_scale,
                 view.smooth, view.member, view.member_spread)
        total += sample(valid, lon, lat, v, ("rate",))["rate"] * dt_h
    return total
