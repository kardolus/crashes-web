"""NYC Crash Map — Starlette app over the live NYC Open Data crashes API.

No database: see socrata.py. Two pages — Hotspots (map + ranked table + KPIs)
and Patterns (Chart.js histograms) — driven by three global, deep-linkable
filters: ?year & ?mode & ?borough.
"""

import logging
import os
import threading

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import socrata
from .ui import page, FAVICON_SVG, CHARTJS, LEAFLET_JS, LEAFLET_CSS


def _JSON(env):
    return JSONResponse(env)


def _filters(r) -> dict:
    info = socrata.available_years()["data"]
    ymax = info.get("max")
    raw_year = r.query_params.get("year")
    year = socrata.valid_year(raw_year, ymax)
    if raw_year is None:
        year = str(info.get("latest_full"))
    return {
        "year": year,
        "mode": socrata.valid_mode(r.query_params.get("mode")),
        "borough": socrata.valid_borough(r.query_params.get("borough")),
        "years": info.get("years", []),
        "latest_full": info.get("latest_full"),
    }


def _ymb(r):
    f = _filters(r)
    return f["year"], f["mode"], f["borough"]


# ───────────────────────── JSON API ─────────────────────────
async def api_summary(r):
    return _JSON(socrata.summary_kpis(*_ymb(r)))


async def api_hotspots(r):
    return _JSON(socrata.dangerous_intersections(*_ymb(r)))


async def api_by_year(r):
    _, m, b = _ymb(r)
    return _JSON(socrata.crashes_by_year(m, b))


async def api_by_hour(r):
    return _JSON(socrata.by_hour(*_ymb(r)))


async def api_by_weekday(r):
    return _JSON(socrata.by_weekday(*_ymb(r)))


async def api_by_month(r):
    return _JSON(socrata.by_month(*_ymb(r)))


async def api_mode_by_year(r):
    _, _m, b = _ymb(r)
    return _JSON(socrata.mode_by_year(b))


async def api_factors(r):
    return _JSON(socrata.top_factors(*_ymb(r)))


async def api_years(r):
    return _JSON(socrata.available_years())


async def api_freshness(r):
    return _JSON(socrata.freshness())


async def favicon(r):
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


async def healthz(r):            # liveness — process is up
    return PlainTextResponse("ok")


async def ready(r):              # readiness — can serve HTTP; does NOT depend on Socrata
    return PlainTextResponse("ready")


async def sourcez(r):            # upstream/source status, reported separately
    fresh = socrata.freshness()
    return JSONResponse({
        "data_through": fresh["data"].get("latest", ""),
        "source_error": fresh["meta"].get("source_error"),
        "cache": socrata.cache_stats(),
    })


# ───────────────────────── Hotspots page (/) ─────────────────────────
_HOTSPOTS_BODY = """
<div class="kpis">
  <div class="kpi"><div class="kpi-n" id="k-crashes">–</div><div class="kpi-l">crashes</div></div>
  <div class="kpi"><div class="kpi-n" id="k-injured">–</div><div class="kpi-l">people injured</div></div>
  <div class="kpi"><div class="kpi-n" id="k-killed">–</div><div class="kpi-l">people killed</div></div>
  <div class="kpi"><div class="kpi-n" id="k-ped">–</div><div class="kpi-l">% pedestrian</div></div>
  <div class="kpi"><div class="kpi-n" id="k-cyc">–</div><div class="kpi-l">% cyclist</div></div>
</div>
<p class="meta" id="meta-line">Crash hotspots · data through <span id="through">…</span>. <span id="mapped"></span></p>
<div class="card">
  <div class="card-head"><h2>Most dangerous locations</h2>
    <span class="legend">severity
      <span class="dot bad"></span>fatal
      <span class="dot warn"></span>≥10 injured
      <span class="dot good"></span>lower
      · size = total injured</span></div>
  <div id="map"></div>
  <p class="note">Locations are crash clusters grouped by rounded coordinates (~11&nbsp;m), min. 3 crashes — “near” the named streets, not verified intersections. “Lower” (green) means least dangerous <em>of the dangerous list</em>, not safe.</p>
</div>
<div class="card">
  <div class="card-head"><h2>Ranked locations</h2>
    <span class="hint legend">hover a row to find it on the map · click a column to sort</span></div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th class="sortable" data-key="label" tabindex="0" aria-sort="none">Location<span class="ind"></span></th>
        <th class="sortable num" data-key="crashes" tabindex="0" aria-sort="none">Crashes<span class="ind"></span></th>
        <th class="sortable num" data-key="injured" tabindex="0" aria-sort="descending">Injured<span class="ind">▼</span></th>
        <th class="sortable num" data-key="killed" tabindex="0" aria-sort="none">Killed<span class="ind"></span></th>
        <th class="sortable" data-key="sev" tabindex="0" aria-sort="none">Severity<span class="ind"></span></th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const isDark = () => document.documentElement.classList.contains('dark');
let map, layer, data = [];
const markers = new Map();
let sort = {key:'injured', dir:'desc'};
const NUMERIC = new Set(['crashes','injured','killed','sev']);

function animate(el, target){
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches){ el.textContent = target.toLocaleString(); return; }
  const dur = 600, t0 = performance.now();
  (function step(t){ const p = Math.min(1,(t-t0)/dur);
    el.textContent = Math.round(target*(1-(1-p)**3)).toLocaleString();
    if (p<1) requestAnimationFrame(step); })(t0);
}
function sevClass(x){ return x.killed>0 ? 'bad' : (x.injured>=10 ? 'warn' : 'good'); }
function sevLabel(x){ return x.killed>0 ? 'fatal' : (x.injured>=10 ? 'severe' : 'lower'); }
function sevRank(x){ return x.killed*100 + x.injured; }
function radius(x){ return Math.max(5, Math.min(22, 4 + 2.2*Math.sqrt(x.injured))); }
function keyVal(x,k){ if (k==='label') return x.label.toLowerCase(); if (k==='sev') return sevRank(x); return x[k]; }

function renderTable(){
  const m = sort.dir==='asc' ? 1 : -1;
  data.sort((a,b)=>{ const va=keyVal(a,sort.key), vb=keyVal(b,sort.key);
    if (va<vb) return -m; if (va>vb) return m; return sevRank(b)-sevRank(a); });
  if (!data.length){ $('rows').innerHTML = '<tr><td colspan="5" class="empty">No crashes match these filters.</td></tr>'; }
  else $('rows').innerHTML = data.map((x,i) =>
    `<tr data-i="${i}">
       <td>${x.label}</td><td class="num">${x.crashes}</td>
       <td class="num">${x.injured}</td><td class="num">${x.killed}</td>
       <td><span class="dot ${sevClass(x)}"></span>${sevLabel(x)}</td>
     </tr>`).join('');
  document.querySelectorAll('th.sortable').forEach(th => {
    const on = th.dataset.key===sort.key;
    th.querySelector('.ind').textContent = on ? (sort.dir==='asc'?'▲':'▼') : '';
    th.setAttribute('aria-sort', on ? (sort.dir==='asc'?'ascending':'descending') : 'none');
  });
}
function setSort(key){
  if (sort.key===key) sort.dir = sort.dir==='asc'?'desc':'asc';
  else sort = {key, dir: NUMERIC.has(key) ? 'desc' : 'asc'};
  renderTable();
}
const thead = document.querySelector('thead');
thead.addEventListener('click', e => { const th=e.target.closest('th.sortable'); if (th) setSort(th.dataset.key); });
thead.addEventListener('keydown', e => { if (e.key==='Enter'||e.key===' '){ const th=e.target.closest('th.sortable'); if (th){ e.preventDefault(); setSort(th.dataset.key); } } });
$('rows').addEventListener('mouseover', e => { const tr=e.target.closest('tr'); const mk=tr&&markers.get(tr.dataset.i);
  if (mk){ mk.setStyle({weight:4, fillOpacity:1}); mk.bringToFront(); } });
$('rows').addEventListener('mouseout', e => { const tr=e.target.closest('tr'); const mk=tr&&markers.get(tr.dataset.i);
  if (mk){ mk.setStyle({weight: data[tr.dataset.i].killed>0?3:2, fillOpacity:.85}); } });

const tileU = () => 'https://{s}.basemaps.cartocdn.com/' + (isDark()?'dark_all':'light_all') + '/{z}/{x}/{y}{r}.png';
let tileLayer;
async function load(){
  api('/api/summary').then(res => {
    const s = res.data; if (!s || s.crashes==null) return;
    animate($('k-crashes'), s.crashes); animate($('k-injured'), s.injured); animate($('k-killed'), s.killed);
    $('k-killed').style.color = s.killed>0 ? 'var(--bad)' : '';
    $('k-ped').textContent = s.pct_ped+'%'; $('k-cyc').textContent = s.pct_cyc+'%';
    $('mapped').textContent = `${(s.mapped_crashes||0).toLocaleString()} of ${(s.crashes||0).toLocaleString()} crashes are mapped.`;
  });
  api('/api/freshness').then(res => { $('through').textContent = res.data.latest || '—'; });

  const res = await api('/api/hotspots');
  data = res.data || [];
  const pts = data.filter(x => x.lat && x.lon);
  if (!map){
    map = L.map('map');
    tileLayer = L.tileLayer(tileU(), {attribution:'© OpenStreetMap, © CARTO', maxZoom:19}).addTo(map);
    window.addEventListener('themechange', () => tileLayer.setUrl(tileU()));
    if (pts.length) map.fitBounds(pts.map(x => [x.lat, x.lon]), {padding:[30,30]});
    else map.setView([40.7128, -74.0060], 11);
    setTimeout(() => map.invalidateSize(), 0);
  }
  if (layer) layer.remove();
  markers.clear();
  layer = L.layerGroup();
  data.forEach((x,i) => {
    if (!x.lat || !x.lon) return;
    const c = css('--'+sevClass(x));
    const mk = L.circleMarker([x.lat, x.lon],
      {radius:radius(x), color:c, fillColor:c, fillOpacity:.85, weight:x.killed>0?3:2});
    mk.bindPopup(`<b>${x.label}</b><br>${x.crashes} crashes · ${x.injured} injured · ${x.killed} killed`
      + `<br>🚶 ${x.ped} · 🚲 ${x.cyc} · 🚗 ${x.mot}`);
    mk.on('mouseover', () => { const tr=document.querySelector(`tr[data-i="${i}"]`); if (tr) tr.classList.add('row-hl'); });
    mk.on('mouseout',  () => { const tr=document.querySelector(`tr[data-i="${i}"]`); if (tr) tr.classList.remove('row-hl'); });
    markers.set(String(i), mk); mk.addTo(layer);
  });
  layer.addTo(map);
  renderTable();
}
load();
window.addEventListener('resize', () => map && map.invalidateSize());
</script>
"""


def hotspots_page(r):
    f = _filters(r)
    head = (f'<link rel="stylesheet" href="{LEAFLET_CSS[0]}" integrity="{LEAFLET_CSS[1]}" crossorigin="anonymous">'
            f'<script src="{LEAFLET_JS[0]}" integrity="{LEAFLET_JS[1]}" crossorigin="anonymous"></script>')
    return HTMLResponse(page("Hotspots", "/", _HOTSPOTS_BODY, f, head_extra=head))


# ───────────────────────── Patterns page (/patterns) ─────────────────────────
_PATTERNS_BODY = """
<p class="meta">Crash patterns · data through <span id="through">…</span>. Click ⓘ on a chart to learn how to read it. Times are New York local.</p>
<div class="grid2">
  <div class="card"><div class="card-head"><h2>Crashes by year</h2><button class="info-btn" data-help="year" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-year"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Crashes by hour of day</h2><button class="info-btn" data-help="hour" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-hour"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Crashes by day of week</h2><button class="info-btn" data-help="dow" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-dow"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Crashes by month</h2><button class="info-btn" data-help="month" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-month"></canvas></div></div>
</div>
<h2 class="section">Who & why</h2>
<div class="grid2">
  <div class="card"><div class="card-head"><h2>Casualties by road user, by year</h2><button class="info-btn" data-help="modeyr" aria-label="About this chart">i</button></div><div class="subtitle" style="margin-bottom:8px">Ignores the road-user filter — always shows all three groups.</div><div class="chart-wrap"><canvas id="c-modeyr"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Top primary factor (vehicle 1)</h2><button class="info-btn" data-help="factors" aria-label="About this chart">i</button></div><div class="chart-wrap tall"><canvas id="c-factors"></canvas></div></div>
</div>
<dialog id="help" class="help">
  <div class="help-hd"><h3 id="help-title"></h3><button class="help-close" aria-label="Close">×</button></div>
  <div class="help-bd" id="help-body"></div>
</dialog>
<script>
const v = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const DOW_ORDER = [1,2,3,4,5,6,0];
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
Chart.defaults.font.family = "'DM Sans', sans-serif";
Chart.defaults.color = v('--meta');
Chart.defaults.maintainAspectRatio = false;
const noLegend = {plugins:{legend:{display:false}}};
function empty(id, msg){ const cw = document.getElementById(id).closest('.chart-wrap');
  if (cw) cw.innerHTML = '<p class="empty">'+(msg||'No data for these filters.')+'</p>'; }

(async () => {
  api('/api/freshness').then(res => { document.getElementById('through').textContent = res.data.latest || '—'; });

  const yr = (await api('/api/by_year')).data;
  if (!yr.length) empty('c-year'); else
  new Chart('c-year', {type:'line', data:{labels:yr.map(x=>x.year),
    datasets:[{data:yr.map(x=>x.crashes), borderColor:v('--accent'), backgroundColor:v('--accent-soft'), tension:.3, fill:true}]},
    options:{...noLegend, scales:{y:{title:{display:true,text:'crashes'}}}}});

  const hr = (await api('/api/by_hour')).data;
  if (!hr.length) empty('c-hour'); else
  new Chart('c-hour', {type:'bar', data:{labels:hr.map(x=>x.hr),
    datasets:[{data:hr.map(x=>x.crashes), backgroundColor:v('--accent')}]},
    options:{...noLegend, scales:{x:{title:{display:true,text:'hour (NY time)'}}, y:{title:{display:true,text:'crashes'}}}}});

  const dw = (await api('/api/by_weekday')).data;
  if (!dw.length) empty('c-dow'); else {
    const by = {}; dw.forEach(x=>by[x.dow]=x.crashes);
    new Chart('c-dow', {type:'bar', data:{labels:DOW_ORDER.map(d=>DOW[d]),
      datasets:[{data:DOW_ORDER.map(d=>by[d]??0), backgroundColor:v('--accent')}]},
      options:{...noLegend, scales:{y:{title:{display:true,text:'crashes'}}}}});
  }

  const mo = (await api('/api/by_month')).data;
  if (!mo.length) empty('c-month'); else
  new Chart('c-month', {type:'bar', data:{labels:MONTHS,
    datasets:[{data:mo.map(x=>x.crashes), backgroundColor:v('--accent')}]},
    options:{...noLegend, scales:{y:{title:{display:true,text:'crashes'}}}}});

  const my = (await api('/api/mode_by_year')).data;
  if (!my.length) empty('c-modeyr'); else
  new Chart('c-modeyr', {type:'bar', data:{labels:my.map(x=>x.year),
    datasets:[{label:'Pedestrian', data:my.map(x=>x.ped), backgroundColor:v('--bad')},
              {label:'Cyclist', data:my.map(x=>x.cyc), backgroundColor:v('--warn')},
              {label:'Motorist', data:my.map(x=>x.mot), backgroundColor:v('--accent')}]},
    options:{scales:{x:{stacked:true}, y:{stacked:true, title:{display:true,text:'people injured + killed'}}}}});

  const fa = (await api('/api/factors')).data;
  if (!fa.length) empty('c-factors'); else
  new Chart('c-factors', {type:'bar', data:{labels:fa.map(x=>x.factor),
    datasets:[{data:fa.map(x=>x.crashes), backgroundColor:v('--accent')}]},
    options:{indexAxis:'y', ...noLegend, scales:{x:{title:{display:true,text:'crashes'}}}}});
})();

const HELP = {
  year: ['Crashes by year', 'Total reported collisions per year for the current road-user and borough filters (the year filter does not apply here — this chart always spans all years). The current year and 2012 are partial, since the dataset starts mid-2012 and lags real time by ~2 weeks.'],
  hour: ['Crashes by hour of day', 'Collisions bucketed by the hour of the crash time (00–23, New York local), summed over the selected year/road-user/borough. The PM rush typically peaks.'],
  dow: ['Crashes by day of week', 'Collisions by day of week (Mon–Sun) for the current filters.'],
  month: ['Crashes by month', 'Collisions by calendar month for the current filters — useful for seasonal patterns.'],
  modeyr: ['Casualties by road user, by year', 'People injured + killed each year, split into pedestrians, cyclists, and motorists (stacked). This chart deliberately ignores the road-user filter so all three groups are always comparable.'],
  factors: ['Top primary factor (vehicle 1)', 'The most common values of “contributing factor, vehicle 1” (excluding “Unspecified”). Note: this is only the primary vehicle’s first listed factor, not all factors across every vehicle in the crash.'],
};
const dlg = document.getElementById('help');
document.querySelectorAll('.info-btn').forEach(b => b.addEventListener('click', () => {
  const [t, body] = HELP[b.dataset.help];
  document.getElementById('help-title').textContent = t;
  document.getElementById('help-body').textContent = body;
  dlg.showModal();
}));
document.querySelector('.help-close').addEventListener('click', () => dlg.close());
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
</script>
"""


def patterns_page(r):
    f = _filters(r)
    head = f'<script src="{CHARTJS[0]}" integrity="{CHARTJS[1]}" crossorigin="anonymous"></script>'
    return HTMLResponse(page("Patterns", "/patterns", _PATTERNS_BODY, f, head_extra=head))


app = Starlette(routes=[
    Route("/", hotspots_page),
    Route("/patterns", patterns_page),
    Route("/favicon.svg", favicon),
    Route("/favicon.ico", favicon),
    Route("/healthz", healthz),
    Route("/ready", ready),
    Route("/sourcez", sourcez),
    Route("/api/summary", api_summary),
    Route("/api/hotspots", api_hotspots),
    Route("/api/by_year", api_by_year),
    Route("/api/by_hour", api_by_hour),
    Route("/api/by_weekday", api_by_weekday),
    Route("/api/by_month", api_by_month),
    Route("/api/mode_by_year", api_mode_by_year),
    Route("/api/factors", api_factors),
    Route("/api/years", api_years),
    Route("/api/freshness", api_freshness),
])


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Warm the default view in the background so the first visitor isn't cold.
    threading.Thread(target=socrata.seed_cache, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
