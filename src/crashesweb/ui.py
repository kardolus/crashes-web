"""Page shell: <head> (fonts, CSS, pinned+SRI Chart.js/Leaflet), top nav with the
three global filters (borough / year / mode), dark mode."""

from pathlib import Path

from .socrata import MODES, BOROUGHS

_CSS = (Path(__file__).parent / "static" / "app.css").read_text()

# Pinned CDN deps with Subresource Integrity (sha384) — same pins as citibike-web.
CHARTJS = ("https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js",
           "sha384-9nhczxUqK87bcKHh20fSQcTGD4qq5GhayNYSYWqwBkINBhOfQLg/P5HG5lF1urn4")
LEAFLET_JS = ("https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js",
              "sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH")
LEAFLET_CSS = ("https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css",
               "sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H")

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700'
    '&family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500'
    '&display=swap" rel="stylesheet">'
)

_NAV_ITEMS = [("/", "Hotspots"), ("/patterns", "Patterns")]

# Favicon: white crossroads (two bars) + center accent dot on a green tile.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="5" fill="#1da46c"/>'
    '<path fill="#fff" d="M10.5 3h3v18h-3z"/><path fill="#fff" d="M3 10.5h18v3H3z"/>'
    '<circle cx="12" cy="12" r="2.4" fill="#1da46c"/></svg>'
)
# Brand logo: crossroads outline, inherits accent via currentColor.
_LOGO_SVG = (
    '<svg class="logo" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" aria-hidden="true">'
    '<path d="M12 3v18M3 12h18"/><circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/></svg>'
)


def _options(items, current):
    return "".join(
        f'<option value="{slug}"{" selected" if slug == current else ""}>{label}</option>'
        for slug, label in items
    )


def _nav(active: str, f: dict) -> str:
    qs = f"?year={f['year']}&mode={f['mode']}&borough={f['borough']}"
    links = "".join(
        f'<a href="{href}{qs}" class="{"active" if href == active else ""}">{label}</a>'
        for href, label in _NAV_ITEMS
    )
    year_opts = '<option value="all"{}>All years</option>'.format(
        " selected" if f["year"] == "all" else "")
    year_opts += "".join(
        f'<option value="{y}"{" selected" if str(y) == str(f["year"]) else ""}>{y}</option>'
        for y in reversed(f["years"])
    )
    return (
        '<div class="top-bar">'
        '  <div class="top-utility">'
        f'    <div class="brand">{_LOGO_SVG} NYC Crash Map</div>'
        '    <div class="top-actions">'
        f'      <select class="nbhd-select" aria-label="Borough" onchange="setFilter(\'borough\',this.value)">{_options(BOROUGHS, f["borough"])}</select>'
        f'      <select class="nbhd-select" aria-label="Year" onchange="setFilter(\'year\',this.value)">{year_opts}</select>'
        f'      <select class="nbhd-select" aria-label="Road user" onchange="setFilter(\'mode\',this.value)">{_options(MODES, f["mode"])}</select>'
        '      <button class="dark-toggle" onclick="toggleDark()" title="Toggle dark mode">◐</button>'
        '    </div>'
        '  </div>'
        f'  <nav class="top-nav">{links}</nav>'
        '</div>'
    )


# JS shared by every page: theme, filter helpers (preserve the other two params),
# and a small fetch wrapper that flips the stale/error banner.
def _common_js(f: dict) -> str:
    return f"""<script>
const P = {{year:"{f['year']}", mode:"{f['mode']}", borough:"{f['borough']}"}};
function setFilter(k,v){{ const q=new URLSearchParams(location.search); q.set(k,v); location.search='?'+q; }}
function withP(u){{ return u + (u.includes('?')?'&':'?') + new URLSearchParams(P); }}
async function api(u){{
  const r = await fetch(withP(u)); const j = await r.json();
  if (j.meta && j.meta.source_error) showBanner(j.meta.stale
    ? 'NYC Open Data is unreachable — showing the last cached numbers.'
    : 'NYC Open Data is currently unavailable. Try again shortly.');
  return j;
}}
function showBanner(msg){{ const b=document.getElementById('banner'); if(b){{ b.textContent=msg; b.classList.add('show'); }} }}
</script>"""


def page(title: str, active: str, body: str, filters: dict, head_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f8f9fa" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d1117" media="(prefers-color-scheme: dark)">
<title>{title} · NYC Crash Map</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
{_FONTS}
<style>{_CSS}</style>
<script>
  if (localStorage.getItem('theme') === 'dark') document.documentElement.classList.add('dark');
  function toggleDark(){{
    document.documentElement.classList.toggle('dark');
    localStorage.setItem('theme', document.documentElement.classList.contains('dark') ? 'dark' : 'light');
  }}
</script>
{_common_js(filters)}
{head_extra}
</head>
<body>
{_nav(active, filters)}
<div id="banner" class="banner" role="alert"></div>
{body}
</body>
</html>"""
