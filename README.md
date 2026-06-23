# crashes-web — NYC Crash Map

A city-wide dashboard of NYC traffic collisions: a map of dangerous locations, a
ranked table, and pattern charts — filterable by **year**, **road user**
(pedestrian / cyclist / motorist), and **borough**. Lives at
**[crashes.kardol.us](https://crashes.kardol.us)**.

It's the live successor to the CSV/folium accident notebooks in
[`opendata`](../opendata) — same source data, but always current and citywide.

## Architecture — local Postgres+PostGIS mirror

The app serves from a **local Postgres+PostGIS mirror** of the NYC Open Data
**"Motor Vehicle Collisions – Crashes"** dataset (`h9gi-nx95`, ~2.27M rows), refreshed
daily by an ingester CronJob. Every page is a fast, indexed SQL aggregation — all-years
hotspots run in ~0.1 s (vs ~4.7 s when this hit the Socrata API live). The mirror also
buys **stable intersection clustering** (so a corner's crashes aggregate instead of
fragmenting), reproducibility, and independence from Socrata uptime/rate-limits.

```
NYC Open Data (Socrata h9gi-nx95)
      │  daily ingest (backfill + delta) + PostGIS clustering
      ▼
crashes-postgres  ──read-only SQL──>  crashes-web (Starlette)  ──>  Browser
```

- **`ingest.py`** — `python -m crashesweb.ingest {backfill|daily}`. Keyset-paginated
  pull from Socrata → upsert into `crashes`. Then assigns each geocoded crash a stable
  `cluster_id` via a **fixed ~30 m grid** (EPSG:2263) and rebuilds the `clusters` table
  (centroid + representative cross-street label). A fixed grid is chaining-proof —
  distance-DBSCAN cascaded crashes along busy corridors into one 27k-crash blob — and a
  30 m cell is wide enough that one corner's scatter lands together. Uses `socrata.py`.
- **`db.py`** — read-only (`crashes_ro`) psycopg2 layer; same `{data, meta}` envelopes
  the frontend expects. Hotspots = `GROUP BY cluster_id` over `crashes JOIN clusters`
  with the year/mode/borough filters, ranked by severity (`killed*100 + injured`).
- **`socrata.py`** — Socrata HTTP client + validators. Now used **only by the ingester**.
- **`server.py`** — Starlette routes, both page bodies (inline JS), health endpoints.
- **`ui.py`** — page shell: fonts, flightdeck CSS, dark mode, the three-filter nav.

**Single replica only** — the query cache is in-process; scaling horizontally would
split it (add Redis first). The DB is host-docker `crashes-postgres` (`:5435`,
postgis/postgis:16), reached via a no-selector Service+Endpoints, in the nightly pg_dump.

## Filters (deep-linkable)

`?year=<YYYY|all>&mode=<all|pedestrian|cyclist|motorist>&borough=<citywide|manhattan|brooklyn|queens|bronx|staten-island>`

Defaults: latest full year · all road users · citywide. The three params are
preserved across navigation and fanned into every `/api/*` call.

## Endpoints

`/` Hotspots (map + ranked table + KPIs) · `/patterns` (charts) ·
`/healthz` (liveness) · `/ready` (readiness — checks the Postgres mirror) ·
`/sourcez` (data freshness + cache stats) · `/api/{summary,hotspots,by_year,
by_hour,by_weekday,by_month,mode_by_year,factors,years,freshness}`.

## Data notes (verified against the live SoQL engine)

- It's the newer **piped** SoQL engine: `round(x)` (1-arg) is **not** supported — use
  `round(x, 4)` to snap coordinates (~11 m).
- The hotspot ranking is `killed*100 + injured` (severity) **with `$having count(*) >= 3`**,
  so a single mass-casualty crash doesn't masquerade as a recurring dangerous location.
- `date_extract_dow` is **0=Sunday**. Numeric aggregates come back as **strings**
  (normalized in `socrata.py`).
- **Map totals < KPI totals**: rows with null/out-of-NYC-bbox coordinates are dropped
  from the map but kept in KPIs (the "N of M crashes mapped" footnote).
- **Borough** is blank on many geocoded rows, so the borough filter undercounts;
  default is citywide and borough-scoped numbers are "rows tagged {borough}".
- Mode filters are **injury/fatality-based** (a crash counts toward "pedestrian" if a
  pedestrian was injured or killed).
- Source lags real time by ~2 weeks; the current year (and 2012) are partial.

### Cold-query benchmarks (anonymous, one-off `curl`)

| Query | Cold time |
|---|---|
| Citywide latest-year summary | ~0.4 s |
| Citywide latest-year hotspots (default view) | ~0.5 s |
| Citywide all-years summary | ~2.0 s |
| Citywide all-years hotspots (heaviest) | ~4.7 s |

TTL policy: current/partial year ~2 h · completed past year ~8 h · all-years ~18 h ·
year list ~24 h. The default view is startup-seeded so the first visitor isn't cold.

## Credentials (optional)

Anonymous access works (with lower rate limits + the in-process cache). To raise the
ceiling, set **either**:
- an **API Key pair** — `OPENDATA_API_KEY_ID` + `OPENDATA_API_KEY_SECRET` (Basic auth), or
- an **App Token** — `OPENDATA_APP_TOKEN` (`X-App-Token`).

> Note: a bare `OPENDATA_API_KEY` is accepted as the secret half of a key pair (needs
> the matching `OPENDATA_API_KEY_ID`). A standalone 49-char NYC "API Key" secret is
> **not** a valid app token — get the Key ID, or generate a classic App Token.

## Develop

```bash
uv run python -m crashesweb.server   # http://localhost:8000
```

## Deploy (forge k8s)

```bash
docker build -t crashes-web:v1 .
docker save crashes-web:v1 | sudo ctr -a /run/k8s-containerd/containerd.sock -n k8s.io images import -
kubectl create namespace crashes
kubectl apply -f deploy/k8s/10-crashes-web.yaml
```

Then add `crashes.kardol.us` to the platform Cloudflare tunnel + a DNS record.
