"""Ingester: NYC Open Data crashes -> Postgres mirror + PostGIS intersection clustering.

    python -m crashesweb.ingest backfill   # full history (keyset-paginated)
    python -m crashesweb.ingest daily       # re-pull a recent window, upsert, recluster

Reuses socrata._get for fetching. After upserting, reclusters all geocoded crashes
with PostGIS ST_ClusterDBSCAN (~30 m, EPSG:2263) so each real intersection gets one
stable cluster_id — crashes at the same corner aggregate instead of fragmenting across
coordinate-rounded cells (the "missing spots" fix). Then rebuilds the clusters table
(centroid + representative street label) the web app reads.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

from . import socrata

log = logging.getLogger("crashesweb.ingest")

DSN = os.environ["CRASHES_DB_URL"]
PAGE = 50000
DAILY_LOOKBACK_DAYS = 21  # re-pull recent window to catch late updates/corrections
CLUSTER_GRID_FT = 100     # ~30 m grid in EPSG:2263 (NY State Plane, feet)

SELECT = ",".join([
    "collision_id", "crash_date", "crash_time", "borough", "latitude", "longitude",
    "on_street_name", "cross_street_name",
    "number_of_persons_injured", "number_of_persons_killed",
    "number_of_pedestrians_injured", "number_of_pedestrians_killed",
    "number_of_cyclist_injured", "number_of_cyclist_killed",
    "number_of_motorist_injured", "number_of_motorist_killed",
    "contributing_factor_vehicle_1",
])

COLS = [
    "collision_id", "crash_ts", "crash_date", "hour", "borough", "lat", "lon",
    "persons_injured", "persons_killed", "ped_injured", "ped_killed",
    "cyc_injured", "cyc_killed", "mot_injured", "mot_killed",
    "factor_1", "on_street", "cross_street",
]


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _coord(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row(r: dict):
    cid = r.get("collision_id")
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None
    date = (r.get("crash_date") or "")[:10]
    if len(date) != 10:
        return None
    t = (r.get("crash_time") or "").strip()
    hour = None
    try:
        h = int(t.split(":")[0])
        if 0 <= h <= 23:
            hour = h
    except (ValueError, IndexError):
        pass
    ts = f"{date} {t}" if (t and hour is not None) else date
    lat, lon = _coord(r.get("latitude")), _coord(r.get("longitude"))
    if lat is None or lon is None or not (40.45 <= lat <= 40.95 and -74.30 <= lon <= -73.65):
        lat = lon = None  # drop nulls / geocoding artifacts -> no geom
    return {
        "collision_id": cid, "crash_ts": ts, "crash_date": date, "hour": hour,
        "borough": (r.get("borough") or None),
        "lat": lat, "lon": lon,
        "persons_injured": _int(r.get("number_of_persons_injured")),
        "persons_killed": _int(r.get("number_of_persons_killed")),
        "ped_injured": _int(r.get("number_of_pedestrians_injured")),
        "ped_killed": _int(r.get("number_of_pedestrians_killed")),
        "cyc_injured": _int(r.get("number_of_cyclist_injured")),
        "cyc_killed": _int(r.get("number_of_cyclist_killed")),
        "mot_injured": _int(r.get("number_of_motorist_injured")),
        "mot_killed": _int(r.get("number_of_motorist_killed")),
        "factor_1": r.get("contributing_factor_vehicle_1") or None,
        "on_street": (r.get("on_street_name") or "").strip() or None,
        "cross_street": (r.get("cross_street_name") or "").strip() or None,
    }


_UPSERT = (
    f"INSERT INTO crashes ({','.join(COLS)}) VALUES %s "
    f"ON CONFLICT (collision_id) DO UPDATE SET "
    + ",".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "collision_id")
)


def _upsert(cur, rows):
    vals = [[r[c] for c in COLS] for r in rows]
    execute_values(cur, _UPSERT, vals, page_size=2000)


def _pages(where_extra=None):
    """Keyset pagination by collision_id (cheap; avoids deep $offset)."""
    last = -1
    while True:
        where = f"collision_id > {last}"
        if where_extra:
            where += f" AND {where_extra}"
        rows = socrata._get(**{"$select": SELECT, "$where": where,
                               "$order": "collision_id", "$limit": PAGE})
        if not rows:
            return
        yield rows
        last = max(int(x["collision_id"]) for x in rows)
        if len(rows) < PAGE:
            return


def recluster(conn):
    """Assign each geocoded crash to a fixed ~30 m grid cell (EPSG:2263, ft), then
    rebuild the clusters table. A fixed grid can't chain (unlike distance-DBSCAN,
    which cascades crashes along busy corridors into mega-blobs), and a 30 m cell is
    wide enough that one corner's scatter lands together (the 11 m round-split missed
    spots). cluster_id = gridX*100000 + gridY (gridY < 100000 in NYC State Plane)."""
    with conn.cursor() as cur:
        log.info("clustering (%d ft grid)…", CLUSTER_GRID_FT)
        cur.execute(f"""
            WITH g AS (
              SELECT collision_id,
                     floor(ST_X(ST_Transform(geom, 2263)) / {CLUSTER_GRID_FT})::bigint * 100000
                     + floor(ST_Y(ST_Transform(geom, 2263)) / {CLUSTER_GRID_FT})::bigint AS cid
              FROM crashes WHERE geom IS NOT NULL
            )
            UPDATE crashes c SET cluster_id = g.cid
            FROM g WHERE c.collision_id = g.collision_id
              AND c.cluster_id IS DISTINCT FROM g.cid
        """)
        log.info("assigned cluster_id to %d changed rows; rebuilding clusters…", cur.rowcount)
        cur.execute("TRUNCATE clusters")
        # Clean street labels: strip leading house numbers ("150-19 BREWER BLVD"),
        # collapse whitespace, drop a redundant "X & X".
        cur.execute("""
            WITH lbl AS (
              SELECT cluster_id, avg(lat) lat, avg(lon) lon,
                     count(*) n, sum(persons_injured) inj, sum(persons_killed) kil,
                     nullif(btrim(regexp_replace(regexp_replace(
                       coalesce(mode() WITHIN GROUP (ORDER BY on_street), ''),
                       '^\\s*\\d+-\\d+\\s+', ''), '\\s+', ' ', 'g')), '') onst,
                     nullif(btrim(regexp_replace(regexp_replace(
                       coalesce(mode() WITHIN GROUP (ORDER BY cross_street), ''),
                       '^\\s*\\d+-\\d+\\s+', ''), '\\s+', ' ', 'g')), '') crst
              FROM crashes WHERE cluster_id IS NOT NULL GROUP BY cluster_id
            )
            INSERT INTO clusters (cluster_id, lat, lon, label, n_crashes, injured, killed)
            SELECT cluster_id, lat, lon,
              CASE WHEN onst IS NOT NULL AND crst IS NOT NULL AND onst <> crst
                     THEN onst || ' & ' || crst
                   WHEN onst IS NOT NULL THEN onst ELSE crst END,
              n, inj, kil FROM lbl
        """)
        cur.execute("SELECT count(*) FROM clusters")
        log.info("clusters rebuilt: %d", cur.fetchone()[0])
    conn.commit()


def _update_state(conn):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ingest_state SET
              last_crash_date = (SELECT max(crash_date) FROM crashes),
              rows_total = (SELECT count(*) FROM crashes),
              updated_at = now()
            WHERE id = 1
        """)
    conn.commit()


def run(mode: str):
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    where_extra = None
    if mode == "daily":
        with conn.cursor() as cur:
            cur.execute("SELECT last_crash_date FROM ingest_state WHERE id=1")
            row = cur.fetchone()
        last = row[0] if row else None
        if last:
            since = last - datetime.timedelta(days=DAILY_LOOKBACK_DAYS)
            where_extra = f"crash_date >= '{since.isoformat()}'"
            log.info("daily: re-pulling crashes since %s", since)
        else:
            log.info("daily: no prior state — full backfill")

    total = 0
    for page in _pages(where_extra):
        rows = [r for r in (_row(x) for x in page) if r]
        with conn.cursor() as cur:
            _upsert(cur, rows)
        conn.commit()
        total += len(rows)
        log.info("upserted %d (running %d)", len(rows), total)

    recluster(conn)
    _update_state(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT rows_total, last_crash_date FROM ingest_state WHERE id=1")
        rt, lcd = cur.fetchone()
    log.info("done: %d rows ingested this run; table now %s rows, through %s", total, rt, lcd)
    conn.close()


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode not in ("backfill", "daily"):
        sys.exit("usage: python -m crashesweb.ingest [backfill|daily]")
    run(mode)


if __name__ == "__main__":
    main()
