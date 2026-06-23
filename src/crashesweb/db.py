"""Postgres data layer for the crashes dashboard (read-only crashes_ro role).

Replaces the live-Socrata passthrough for serving: every page is now a fast,
indexed SQL aggregation over the local mirror (crashes + clusters tables, filled
daily by ingest.py). Same public function names and {data, meta} envelopes as the
old socrata.py so server.py is unchanged apart from the import.

Hotspots come from the precomputed, filter-independent cluster_id (PostGIS DBSCAN),
so a real intersection's crashes aggregate instead of fragmenting — and we can rank
the full set by severity without an artificial min-crash floor or coordinate split.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

# pure validators/constants reused from the (now ingester-only) socrata module
from .socrata import MODES, BOROUGHS, valid_year, valid_mode, valid_borough, MIN_YEAR  # noqa: F401

log = logging.getLogger("crashesweb.db")

_MODE_SQL = {
    "pedestrian": "(ped_injured>0 OR ped_killed>0)",
    "cyclist": "(cyc_injured>0 OR cyc_killed>0)",
    "motorist": "(mot_injured>0 OR mot_killed>0)",
}
_BOROUGH_UP = {"manhattan": "MANHATTAN", "brooklyn": "BROOKLYN", "queens": "QUEENS",
               "bronx": "BRONX", "staten-island": "STATEN ISLAND"}


def _where(year, mode, borough, prefix=""):
    """Return (sql_conditions, params). `prefix` aliases columns (e.g. 'c.')."""
    cond, params = [], []
    if year and year != "all":
        y = int(year)
        cond.append(f"{prefix}crash_date >= %s AND {prefix}crash_date < %s")
        params += [f"{y}-01-01", f"{y + 1}-01-01"]
    if mode in _MODE_SQL:
        cond.append(_MODE_SQL[mode] if not prefix else _MODE_SQL[mode].replace(
            "ped_", f"{prefix}ped_").replace("cyc_", f"{prefix}cyc_").replace("mot_", f"{prefix}mot_"))
    if borough in _BOROUGH_UP:
        cond.append(f"{prefix}borough = %s")
        params.append(_BOROUGH_UP[borough])
    return (" AND ".join(cond) if cond else "TRUE", params)


# ───────────────────────── pool + cache ─────────────────────────
_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, 5, os.environ["CRASHES_DB_URL"])
    return _pool


def _query(sql, params=()):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        pool.putconn(conn, close=True)
        conn = None
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


_cache: dict[str, tuple[float, object]] = {}
_last_good: dict[str, object] = {}
_lock = threading.Lock()
import time  # noqa: E402


def _q(key, ttl, producer, empty):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return {"data": hit[1], "meta": {"stale": False}}
    try:
        data = producer()
    except Exception as e:
        log.warning("db query failed (%s): %s", key, e)
        with _lock:
            lg = _last_good.get(key)
        if lg is not None:
            return {"data": lg, "meta": {"stale": True, "source_error": "db_unavailable"}}
        return {"data": empty, "meta": {"stale": False, "source_error": "db_unavailable"}}
    with _lock:
        _cache[key] = (time.time(), data)
        _last_good[key] = data
    return {"data": data, "meta": {"stale": False}}


def cache_stats() -> dict:
    return {"keys": len(_cache)}


def ping() -> bool:
    _query("SELECT 1")
    return True


def _i(v):
    return int(v) if v is not None else 0


# ───────────────────────── queries ─────────────────────────
def available_years():
    def produce():
        r = _query("SELECT min(crash_date) lo, max(crash_date) hi FROM crashes")[0]
        if not r["hi"]:
            raise RuntimeError("empty crashes table")
        ymax, hi = r["hi"].year, r["hi"]
        latest_full = ymax if hi.month == 12 else ymax - 1
        return {"years": list(range(MIN_YEAR, ymax + 1)), "max": ymax,
                "latest_full": max(latest_full, MIN_YEAR), "data_through": hi.isoformat()}
    return _q("years", 6 * 3600, produce,
              {"years": list(range(MIN_YEAR, datetime.date.today().year + 1)),
               "max": datetime.date.today().year, "latest_full": datetime.date.today().year - 1,
               "data_through": ""})


def freshness():
    def produce():
        r = _query("SELECT max(crash_date) hi FROM crashes")[0]
        return {"latest": r["hi"].isoformat() if r["hi"] else ""}
    return _q("freshness", 3600, produce, {"latest": ""})


def summary_kpis(year, mode, borough):
    key = f"sum:{year}:{mode}:{borough}"

    def produce():
        w, p = _where(year, mode, borough)
        r = _query(f"""
            SELECT count(*) crashes, coalesce(sum(persons_injured),0) injured,
                   coalesce(sum(persons_killed),0) killed,
                   coalesce(sum(ped_injured+ped_killed),0) ped,
                   coalesce(sum(cyc_injured+cyc_killed),0) cyc,
                   coalesce(sum(mot_injured+mot_killed),0) mot,
                   count(*) FILTER (WHERE geom IS NOT NULL) mapped
            FROM crashes WHERE {w}""", p)[0]
        ped, cyc, mot = _i(r["ped"]), _i(r["cyc"]), _i(r["mot"])
        cas = ped + cyc + mot
        pct = lambda n: round(100 * n / cas) if cas else 0
        return {"crashes": _i(r["crashes"]), "injured": _i(r["injured"]), "killed": _i(r["killed"]),
                "ped": ped, "cyc": cyc, "mot": mot,
                "pct_ped": pct(ped), "pct_cyc": pct(cyc), "pct_mot": pct(mot),
                "mapped_crashes": _i(r["mapped"])}
    return _q(key, 3600, produce, {})


def dangerous_intersections(year, mode, borough, limit=500):
    key = f"hot:{year}:{mode}:{borough}:{limit}"

    def produce():
        w, p = _where(year, mode, borough, prefix="c.")
        rows = _query(f"""
            SELECT cl.lat, cl.lon, cl.label,
                   count(*) crashes,
                   coalesce(sum(c.persons_injured),0) injured,
                   coalesce(sum(c.persons_killed),0) killed,
                   coalesce(sum(c.ped_injured+c.ped_killed),0) ped,
                   coalesce(sum(c.cyc_injured+c.cyc_killed),0) cyc,
                   coalesce(sum(c.mot_injured+c.mot_killed),0) mot
            FROM crashes c JOIN clusters cl USING (cluster_id)
            WHERE c.cluster_id IS NOT NULL AND {w}
            GROUP BY cl.cluster_id, cl.lat, cl.lon, cl.label
            ORDER BY (coalesce(sum(c.persons_killed),0)*100 + coalesce(sum(c.persons_injured),0)) DESC
            LIMIT %s""", p + [int(limit)])
        out = []
        for r in rows:
            lat, lon = r["lat"], r["lon"]
            label = (r["label"] or "").title() or f"near {lat:.4f}, {lon:.4f}"
            out.append({"lat": lat, "lon": lon, "label": label,
                        "crashes": _i(r["crashes"]), "injured": _i(r["injured"]),
                        "killed": _i(r["killed"]), "ped": _i(r["ped"]),
                        "cyc": _i(r["cyc"]), "mot": _i(r["mot"])})
        return out
    return _q(key, 3600, produce, [])


def crashes_by_year(mode, borough):
    key = f"byyear:{mode}:{borough}"

    def produce():
        w, p = _where("all", mode, borough)
        rows = _query(f"""
            SELECT extract(year from crash_date)::int yr, count(*) crashes,
                   coalesce(sum(persons_injured),0) injured, coalesce(sum(persons_killed),0) killed
            FROM crashes WHERE {w} GROUP BY 1 ORDER BY 1""", p)
        return [{"year": _i(r["yr"]), "crashes": _i(r["crashes"]),
                 "injured": _i(r["injured"]), "killed": _i(r["killed"])}
                for r in rows if _i(r["yr"]) >= MIN_YEAR]
    return _q(key, 6 * 3600, produce, [])


def by_hour(year, mode, borough):
    key = f"hour:{year}:{mode}:{borough}"

    def produce():
        w, p = _where(year, mode, borough)
        rows = _query(f"SELECT hour, count(*) n FROM crashes WHERE {w} AND hour IS NOT NULL GROUP BY hour", p)
        by = {_i(r["hour"]): _i(r["n"]) for r in rows}
        return [{"hr": h, "crashes": by.get(h, 0)} for h in range(24)]
    return _q(key, 3600, produce, [])


def by_weekday(year, mode, borough):
    key = f"dow:{year}:{mode}:{borough}"

    def produce():
        w, p = _where(year, mode, borough)
        rows = _query(f"SELECT extract(dow from crash_date)::int dow, count(*) n FROM crashes WHERE {w} GROUP BY 1", p)
        by = {_i(r["dow"]): _i(r["n"]) for r in rows}  # 0=Sunday
        return [{"dow": d, "crashes": by.get(d, 0)} for d in range(7)]
    return _q(key, 3600, produce, [])


def by_month(year, mode, borough):
    key = f"month:{year}:{mode}:{borough}"

    def produce():
        w, p = _where(year, mode, borough)
        rows = _query(f"SELECT extract(month from crash_date)::int m, count(*) n FROM crashes WHERE {w} GROUP BY 1", p)
        by = {_i(r["m"]): _i(r["n"]) for r in rows}
        return [{"month": m, "crashes": by.get(m, 0)} for m in range(1, 13)]
    return _q(key, 3600, produce, [])


def mode_by_year(borough):
    key = f"modeyr:{borough}"

    def produce():
        w, p = _where("all", "all", borough)
        rows = _query(f"""
            SELECT extract(year from crash_date)::int yr,
                   coalesce(sum(ped_injured+ped_killed),0) ped,
                   coalesce(sum(cyc_injured+cyc_killed),0) cyc,
                   coalesce(sum(mot_injured+mot_killed),0) mot
            FROM crashes WHERE {w} GROUP BY 1 ORDER BY 1""", p)
        return [{"year": _i(r["yr"]), "ped": _i(r["ped"]), "cyc": _i(r["cyc"]), "mot": _i(r["mot"])}
                for r in rows if _i(r["yr"]) >= MIN_YEAR]
    return _q(key, 6 * 3600, produce, [])


def top_factors(year, mode, borough, limit=10):
    key = f"factors:{year}:{mode}:{borough}:{limit}"

    def produce():
        w, p = _where(year, mode, borough)
        rows = _query(f"""
            SELECT factor_1 factor, count(*) crashes FROM crashes
            WHERE {w} AND factor_1 IS NOT NULL AND factor_1 <> 'Unspecified'
            GROUP BY factor_1 ORDER BY 2 DESC LIMIT %s""", p + [int(limit)])
        return [{"factor": r["factor"], "crashes": _i(r["crashes"])} for r in rows]
    return _q(key, 6 * 3600, produce, [])
