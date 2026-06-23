# crashes-web — NYC Crash Map

A city-wide dashboard of NYC traffic collisions: a map of dangerous locations, a
ranked table, and pattern charts — filterable by **year**, **road user**
(pedestrian / cyclist / motorist), and **borough**. Lives at
**[crashes.kardol.us](https://crashes.kardol.us)**.

It's the live successor to the CSV/folium accident notebooks in
[`opendata`](../opendata) — same source data, but always current and citywide.

## Architecture — cached live passthrough (no database)

The app is a **cached live [Socrata](https://dev.socrata.com/) dashboard** for
low-traffic homelab use. It reads the NYC Open Data **"Motor Vehicle Collisions –
Crashes"** dataset (`h9gi-nx95`, ~2.27M rows) directly and lets the SoQL engine do
the aggregation server-side; nothing is stored locally. It avoids an ingester/DB at
the cost of cold-cache latency, source dependency, coordinate-rounded (not true)
intersection clustering, and limited reproducibility.

```
Browser ──> crashes-web (Starlette) ──cached HTTP──> NYC Open Data (Socrata h9gi-nx95)
```

- **`socrata.py`** — HTTP client + query functions. Each query is wrapped in an
  in-process TTL cache with **per-key request coalescing** (concurrent misses for one
  key collapse to a single upstream call), a **global semaphore** (≤3 concurrent
  outbound calls), and **stale-on-error** (serve the last good value if Socrata
  blips). The cache lock is never held across the HTTP call. Returns an envelope
  `{"data": ..., "meta": {"stale": bool, "source_error": str|None}}`.
- **`server.py`** — Starlette routes, both page bodies (inline JS), health endpoints.
- **`ui.py`** — page shell: fonts, flightdeck CSS, dark mode, the three-filter nav.

**Single replica only** — the cache is in-process. Scaling horizontally would double
upstream traffic and split the cache; add Redis/a file cache first.

### Migration triggers → build a Postgres/PostGIS mirror if:
- cold all-years hotspots regularly exceeds ~5–8 s;
- Socrata 429/5xx occur in normal use;
- you need a 2nd replica;
- you need real intersection identity, borough polygons, or multi-column factor counts.

## Filters (deep-linkable)

`?year=<YYYY|all>&mode=<all|pedestrian|cyclist|motorist>&borough=<citywide|manhattan|brooklyn|queens|bronx|staten-island>`

Defaults: latest full year · all road users · citywide. The three params are
preserved across navigation and fanned into every `/api/*` call.

## Endpoints

`/` Hotspots (map + ranked table + KPIs) · `/patterns` (charts) ·
`/healthz` (liveness) · `/ready` (readiness — independent of Socrata) ·
`/sourcez` (upstream freshness + cache stats) · `/api/{summary,hotspots,by_year,
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
