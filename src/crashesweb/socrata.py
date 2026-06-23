"""Live access to NYC Open Data "Motor Vehicle Collisions - Crashes" (h9gi-nx95).

There is NO database. Every page is an aggregation the Socrata SoQL engine
computes server-side over the full ~2.27M-row citywide dataset; we wrap each
query in a small in-process TTL cache with:

  - per-key request coalescing (concurrent misses for one key -> one upstream call)
  - a global semaphore (<=3 concurrent outbound calls; a cold pod can't fan out)
  - stale-on-error (serve the last good value if Socrata blips, tagged stale)

so public page loads don't re-hit the API and a source outage degrades instead
of 500-ing. The lock is never held across the HTTP call.

Verified against the live (piped) SoQL engine (see README "Data notes"):
  * round(x, 4) snaps coords to ~11 m; round(x) 1-arg is NOT supported
  * $having count(*) >= N drops one-off mass-casualty points from the ranking
  * date_extract_dow is 0=Sunday .. 6=Saturday
  * numeric aggregates come back as STRINGS -> normalized via _i()/_f()

Every public function returns an ENVELOPE: {"data": <payload>, "meta": {...}}.
"""

from __future__ import annotations

import base64
import datetime
import logging
import os
import threading
import time

import httpx

log = logging.getLogger("crashesweb.socrata")

RESOURCE = "https://data.cityofnewyork.us/resource/h9gi-nx95.json"
MIN_YEAR = 2012  # dataset starts mid-2012
# Loose NYC bounding box — drops geocoding artifacts (e.g. (0,0) or bad longitude).
NYC_BBOX = "latitude between 40.45 and 40.95 and longitude between -74.30 and -73.65"

# ───────────────────────── filters (validated/clamped) ─────────────────────────
MODES = [
    ("all", "All road users"),
    ("pedestrian", "Pedestrians"),
    ("cyclist", "Cyclists"),
    ("motorist", "Motorists"),
]
BOROUGHS = [
    ("citywide", "Citywide"),
    ("manhattan", "Manhattan"),
    ("brooklyn", "Brooklyn"),
    ("queens", "Queens"),
    ("bronx", "Bronx"),
    ("staten-island", "Staten Island"),
]
_MODE_WHERE = {
    "pedestrian": "(number_of_pedestrians_injured>0 OR number_of_pedestrians_killed>0)",
    "cyclist": "(number_of_cyclist_injured>0 OR number_of_cyclist_killed>0)",
    "motorist": "(number_of_motorist_injured>0 OR number_of_motorist_killed>0)",
}
_BOROUGH_WHERE = {
    "manhattan": "MANHATTAN", "brooklyn": "BROOKLYN", "queens": "QUEENS",
    "bronx": "BRONX", "staten-island": "STATEN ISLAND",
}
_MODE_SLUGS = {s for s, _ in MODES}
_BOROUGH_SLUGS = {s for s, _ in BOROUGHS}


def _current_year() -> int:
    return datetime.datetime.now(datetime.UTC).year


def valid_mode(s: str | None) -> str:
    return s if s in _MODE_SLUGS else "all"


def valid_borough(s: str | None) -> str:
    return s if s in _BOROUGH_SLUGS else "citywide"


def valid_year(s: str | None, ymax: int | None = None) -> str:
    """Clamp ?year= to 'all' or an int in [MIN_YEAR, ymax]."""
    if s == "all":
        return "all"
    try:
        y = int(s)
    except (TypeError, ValueError):
        return "all"
    hi = ymax or _current_year()
    return str(y) if MIN_YEAR <= y <= hi else "all"


def _where(year, mode, borough, geo=False, extra=None) -> str:
    parts = []
    if year and year != "all":
        y = int(year)
        parts.append(f"crash_date between '{y}-01-01T00:00:00' and '{y}-12-31T23:59:59'")
    if mode in _MODE_WHERE:
        parts.append(_MODE_WHERE[mode])
    if borough in _BOROUGH_WHERE:
        parts.append(f"borough='{_BOROUGH_WHERE[borough]}'")
    if geo:
        parts.append(NYC_BBOX)
    if extra:
        parts.append(extra)
    return " AND ".join(parts) if parts else "1=1"


def _ttl(year) -> float:
    """all-years = longest (cheap-to-stale, expensive-to-compute); current year = short."""
    if year == "all":
        return 18 * 3600
    return 2 * 3600 if int(year) >= _current_year() else 8 * 3600


# ───────────────────────── HTTP client + auth ─────────────────────────
_TIMEOUT = float(os.environ.get("SOCRATA_TIMEOUT", "12"))


def _auth_kwargs() -> dict:
    """Socrata has two schemes. Prefer an API-key pair (Basic auth); fall back to
    an app token header. Anonymous (nothing set) works too — just lower limits.

      * API key pair:  OPENDATA_API_KEY_ID + OPENDATA_API_KEY(_SECRET)  -> Basic
      * App token:     OPENDATA_APP_TOKEN (or OPENDATA_API_KEY)         -> X-App-Token
    """
    headers = {"User-Agent": "crashes.kardol.us (homelab dashboard; kardolus@gmail.com)"}
    key_id = os.environ.get("OPENDATA_API_KEY_ID")
    secret = os.environ.get("OPENDATA_API_KEY_SECRET") or os.environ.get("OPENDATA_API_KEY")
    if key_id and secret:
        token = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
        return {"headers": headers}
    app_token = os.environ.get("OPENDATA_APP_TOKEN")
    if app_token:
        headers["X-App-Token"] = app_token
    return {"headers": headers}


_client = httpx.Client(
    timeout=httpx.Timeout(_TIMEOUT),
    transport=httpx.HTTPTransport(retries=2),
    limits=httpx.Limits(max_connections=8),
    **_auth_kwargs(),
)


class SocrataError(Exception):
    pass


def _get(**params) -> list[dict]:
    """One SoQL GET. Numbers come back as strings — callers normalize."""
    params.setdefault("$limit", 50000)
    t0 = time.monotonic()
    try:
        r = _client.get(RESOURCE, params=params)
        r.raise_for_status()
        rows = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("socrata error (%.2fs): %s", time.monotonic() - t0, e)
        raise SocrataError(str(e)) from e
    log.info("socrata ok %.2fs rows=%d where=%s", time.monotonic() - t0,
             len(rows), params.get("$where", "")[:80])
    return rows


def _i(row, key, default=0) -> int:
    try:
        return int(float(row[key]))
    except (KeyError, TypeError, ValueError):
        return default


def _f(row, key, default=0.0) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return default


# ───────────────────────── cache (coalescing + stale-on-error) ─────────────────────────
_cache: dict[str, tuple[float, object]] = {}   # key -> (ts, data)
_last_good: dict[str, object] = {}              # key -> data (for stale-on-error)
_cache_lock = threading.Lock()
_key_locks: dict[str, threading.Lock] = {}
_key_locks_lock = threading.Lock()
_SEM = threading.Semaphore(int(os.environ.get("SOCRATA_MAX_CONCURRENCY", "3")))
_hits = _misses = _stale = 0


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_lock:
        lk = _key_locks.get(key)
        if lk is None:
            lk = _key_locks[key] = threading.Lock()
        return lk


def _q(key: str, ttl: float, producer, empty):
    """Return an envelope {data, meta}. producer() returns plain data (may raise
    SocrataError). The cache lock is never held across producer()."""
    global _hits, _misses, _stale
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            _hits += 1
            return {"data": hit[1], "meta": {"stale": False}}
    with _key_lock(key):
        # re-check: a concurrent caller may have just filled it
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            _hits += 1
            return {"data": hit[1], "meta": {"stale": False}}
        _misses += 1
        try:
            with _SEM:
                data = producer()
        except SocrataError:
            with _cache_lock:
                lg = _last_good.get(key)
            if lg is not None:
                _stale += 1
                return {"data": lg, "meta": {"stale": True, "source_error": "source_unavailable"}}
            return {"data": empty, "meta": {"stale": False, "source_error": "source_unavailable"}}
        with _cache_lock:
            _cache[key] = (time.time(), data)
            _last_good[key] = data
        return {"data": data, "meta": {"stale": False}}


def cache_stats() -> dict:
    return {"hits": _hits, "misses": _misses, "stale_served": _stale, "keys": len(_cache)}


# ───────────────────────── query functions (return envelopes) ─────────────────────────
def available_years():
    """Year dropdown source — STATIC 2012..max so options don't shift with filters."""
    def produce():
        rows = _get(**{"$select": "max(crash_date) as latest"})
        latest = (rows[0].get("latest") or "")[:10] if rows else ""
        ymax = int(latest[:4]) if latest[:4].isdigit() else _current_year()
        mmax = int(latest[5:7]) if latest[5:7].isdigit() else 1
        latest_full = ymax if mmax == 12 else ymax - 1
        return {
            "years": list(range(MIN_YEAR, ymax + 1)),
            "max": ymax,
            "latest_full": max(latest_full, MIN_YEAR),
            "data_through": latest,
        }
    return _q("years", 24 * 3600, produce,
              {"years": list(range(MIN_YEAR, _current_year() + 1)),
               "max": _current_year(), "latest_full": _current_year() - 1, "data_through": ""})


def freshness():
    def produce():
        rows = _get(**{"$select": "max(crash_date) as latest"})
        return {"latest": (rows[0].get("latest") or "")[:10] if rows else ""}
    return _q("freshness", 6 * 3600, produce, {"latest": ""})


def dangerous_intersections(year, mode, borough, limit=300, min_crashes=3):
    key = f"hot:{year}:{mode}:{borough}:{limit}:{min_crashes}"

    def produce():
        sev = "(sum(number_of_persons_killed)*100 + sum(number_of_persons_injured))"
        rows = _get(**{
            "$select": (
                "round(latitude,4) as lat, round(longitude,4) as lon,"
                "max(on_street_name) as on_st, max(cross_street_name) as cross_st,"
                "count(*) as crashes,"
                "sum(number_of_persons_injured) as injured,"
                "sum(number_of_persons_killed) as killed,"
                "sum(number_of_pedestrians_injured + number_of_pedestrians_killed) as ped,"
                "sum(number_of_cyclist_injured   + number_of_cyclist_killed)   as cyc,"
                "sum(number_of_motorist_injured  + number_of_motorist_killed)  as mot"
            ),
            "$where": _where(year, mode, borough, geo=True),
            "$group": "round(latitude,4), round(longitude,4)",
            "$having": f"count(*) >= {int(min_crashes)}",
            "$order": f"{sev} DESC",
            "$limit": max(1, min(int(limit), 500)),
        })
        out = []
        for r in rows:
            lat, lon = _f(r, "lat"), _f(r, "lon")
            on_st = (r.get("on_st") or "").strip().title()
            cross_st = (r.get("cross_st") or "").strip().title()
            if on_st and cross_st:
                label = f"{on_st} & {cross_st}"
            elif on_st:
                label = on_st
            else:
                label = f"near {lat:.4f}, {lon:.4f}"
            out.append({
                "lat": lat, "lon": lon, "label": label,
                "crashes": _i(r, "crashes"), "injured": _i(r, "injured"),
                "killed": _i(r, "killed"), "ped": _i(r, "ped"),
                "cyc": _i(r, "cyc"), "mot": _i(r, "mot"),
            })
        return out
    return _q(key, _ttl(year), produce, [])


def summary_kpis(year, mode, borough):
    key = f"sum:{year}:{mode}:{borough}"

    def produce():
        agg = _get(**{
            "$select": (
                "count(*) as crashes,"
                "sum(number_of_persons_injured) as injured,"
                "sum(number_of_persons_killed) as killed,"
                "sum(number_of_pedestrians_injured + number_of_pedestrians_killed) as ped,"
                "sum(number_of_cyclist_injured   + number_of_cyclist_killed)   as cyc,"
                "sum(number_of_motorist_injured  + number_of_motorist_killed)  as mot"
            ),
            "$where": _where(year, mode, borough),
        })
        a = agg[0] if agg else {}
        mapped = _get(**{"$select": "count(*) as n",
                         "$where": _where(year, mode, borough, geo=True)})
        ped, cyc, mot = _i(a, "ped"), _i(a, "cyc"), _i(a, "mot")
        cas = ped + cyc + mot
        pct = lambda n: round(100 * n / cas) if cas else 0
        return {
            "crashes": _i(a, "crashes"), "injured": _i(a, "injured"),
            "killed": _i(a, "killed"), "ped": ped, "cyc": cyc, "mot": mot,
            "pct_ped": pct(ped), "pct_cyc": pct(cyc), "pct_mot": pct(mot),
            "mapped_crashes": _i(mapped[0], "n") if mapped else 0,
        }
    return _q(key, _ttl(year), produce, {})


def crashes_by_year(mode, borough):
    key = f"byyear:{mode}:{borough}"

    def produce():
        rows = _get(**{
            "$select": ("date_extract_y(crash_date) as year, count(*) as crashes,"
                        "sum(number_of_persons_injured) as injured,"
                        "sum(number_of_persons_killed) as killed"),
            "$where": _where("all", mode, borough),
            "$group": "date_extract_y(crash_date)",
            "$order": "year",
        })
        return [{"year": _i(r, "year"), "crashes": _i(r, "crashes"),
                 "injured": _i(r, "injured"), "killed": _i(r, "killed")}
                for r in rows if _i(r, "year") >= MIN_YEAR]
    return _q(key, 12 * 3600, produce, [])


def by_hour(year, mode, borough):
    key = f"hour:{year}:{mode}:{borough}"

    def produce():
        rows = _get(**{"$select": "crash_time, count(*) as n",
                       "$where": _where(year, mode, borough),
                       "$group": "crash_time", "$limit": 50000})
        buckets = [0] * 24
        for r in rows:
            t = (r.get("crash_time") or "").strip()
            try:
                h = int(t.split(":")[0])
            except (ValueError, IndexError):
                continue
            if 0 <= h <= 23:
                buckets[h] += _i(r, "n")
        return [{"hr": h, "crashes": buckets[h]} for h in range(24)]
    return _q(key, _ttl(year), produce, [])


def by_weekday(year, mode, borough):
    key = f"dow:{year}:{mode}:{borough}"

    def produce():
        rows = _get(**{"$select": "date_extract_dow(crash_date) as dow, count(*) as n",
                       "$where": _where(year, mode, borough),
                       "$group": "date_extract_dow(crash_date)"})
        by = {_i(r, "dow"): _i(r, "n") for r in rows}  # 0=Sunday
        return [{"dow": d, "crashes": by.get(d, 0)} for d in range(7)]
    return _q(key, _ttl(year), produce, [])


def by_month(year, mode, borough):
    key = f"month:{year}:{mode}:{borough}"

    def produce():
        rows = _get(**{"$select": "date_extract_m(crash_date) as m, count(*) as n",
                       "$where": _where(year, mode, borough),
                       "$group": "date_extract_m(crash_date)"})
        by = {_i(r, "m"): _i(r, "n") for r in rows}
        return [{"month": m, "crashes": by.get(m, 0)} for m in range(1, 13)]
    return _q(key, _ttl(year), produce, [])


def mode_by_year(borough):
    """Ped/cyclist/motorist casualties by year — ignores the mode filter by design."""
    key = f"modeyr:{borough}"

    def produce():
        rows = _get(**{
            "$select": ("date_extract_y(crash_date) as year,"
                        "sum(number_of_pedestrians_injured + number_of_pedestrians_killed) as ped,"
                        "sum(number_of_cyclist_injured + number_of_cyclist_killed) as cyc,"
                        "sum(number_of_motorist_injured + number_of_motorist_killed) as mot"),
            "$where": _where("all", "all", borough),
            "$group": "date_extract_y(crash_date)", "$order": "year",
        })
        return [{"year": _i(r, "year"), "ped": _i(r, "ped"),
                 "cyc": _i(r, "cyc"), "mot": _i(r, "mot")}
                for r in rows if _i(r, "year") >= MIN_YEAR]
    return _q(key, 12 * 3600, produce, [])


def top_factors(year, mode, borough, limit=10):
    key = f"factors:{year}:{mode}:{borough}:{limit}"

    def produce():
        rows = _get(**{
            "$select": "contributing_factor_vehicle_1 as factor, count(*) as crashes",
            "$where": _where(year, mode, borough,
                             extra="contributing_factor_vehicle_1 IS NOT NULL "
                                   "AND contributing_factor_vehicle_1 NOT IN ('Unspecified')"),
            "$group": "contributing_factor_vehicle_1",
            "$order": "crashes DESC", "$limit": max(1, min(int(limit), 25)),
        })
        return [{"factor": r.get("factor") or "Unknown", "crashes": _i(r, "crashes")}
                for r in rows]
    return _q(key, 12 * 3600, produce, [])


# ───────────────────────── startup cache warming ─────────────────────────
def seed_cache():
    """Warm the default landing view so the first visitor after a deploy/restart
    doesn't eat a cold multi-second load. Best-effort; failures are ignored."""
    try:
        info = available_years()["data"]
        y = str(info.get("latest_full", _current_year() - 1))
        summary_kpis(y, "all", "citywide")
        dangerous_intersections(y, "all", "citywide")
        crashes_by_year("all", "citywide")
        freshness()
        log.info("cache seeded for default view (year=%s)", y)
    except Exception as e:  # never let warming crash startup
        log.warning("cache seed failed: %s", e)
