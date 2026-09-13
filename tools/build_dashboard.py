"""Generate the operations dashboard as one self-contained file.

Everything is inlined, so the file opens from disk with no server, no account
and no network: copy it to a phone, a USB stick or an email and it still works.
The current rows are baked in rather than fetched, because a dashboard that
opens empty waiting on a request shows nothing.

If the file happens to sit beside a fresh status.json — which it does when the
local console serves it — the page picks that up every 30 seconds and stops
being a snapshot. Neither mode depends on the other.

    python tools/build_dashboard.py [output.html]
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dashboard.html")

snapshot = json.loads(
    subprocess.run([os.path.join(ROOT, ".venv/bin/python"),
                    os.path.join(ROOT, "tools/pushstatus.py")],
                   capture_output=True, text=True, cwd=ROOT).stdout
)

HTML = """<title>Job Search Console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#EFF3F5; --surface:#FFFFFF; --sunk:#E4EAED; --raised:#F7FAFB;
  --ink:#0E1A20; --ink-2:#3A4A53; --muted:#66767F; --faint:#94A3AA;
  --line:#D8E0E4; --hair:#E8EDEF;
  --signal:#0B6E77; --signal-soft:#DFEFF0; --signal-ink:#075860;
  --good:#2C7049; --warn:#8A5A12; --crit:#A63D2C;
  --gh:#2C7049; --lv:#3E5C8C; --ab:#7A4A86; --wd:#B06A1F;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --cond:"IBM Plex Sans Condensed","IBM Plex Sans",system-ui,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0B1317; --surface:#141F25; --sunk:#101A1F; --raised:#18242B;
    --ink:#E7EEF1; --ink-2:#B6C5CC; --muted:#849098; --faint:#5D6C74;
    --line:#223037; --hair:#1A262C;
    --signal:#4FB3BC; --signal-soft:#0F3034; --signal-ink:#7FCCD3;
    --good:#5FAF7E; --warn:#C89440; --crit:#D4705C;
    --gh:#5FAF7E; --lv:#7E9FD4; --ab:#B486C0; --wd:#D19A55;
  }
}
:root[data-theme="dark"]{
  --ground:#0B1317; --surface:#141F25; --sunk:#101A1F; --raised:#18242B;
  --ink:#E7EEF1; --ink-2:#B6C5CC; --muted:#849098; --faint:#5D6C74;
  --line:#223037; --hair:#1A262C;
  --signal:#4FB3BC; --signal-soft:#0F3034; --signal-ink:#7FCCD3;
  --good:#5FAF7E; --warn:#C89440; --crit:#D4705C;
  --gh:#5FAF7E; --lv:#7E9FD4; --ab:#B486C0; --wd:#D19A55;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);
  font-size:14.5px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding-block:28px 64px;padding-left:20px;padding-right:20px}
h1{font-family:var(--cond);font-size:26px;font-weight:700;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13.5px;margin-top:3px}

/* masthead + live */
.top{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;
  padding-bottom:14px;border-bottom:2px solid var(--ink)}
.live{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11.5px;
  color:var(--muted);white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:var(--good)}
@media (prefers-reduced-motion:no-preference){
  .dot.on{animation:pulse 2.4s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
}

/* stat strip — numbers are the point here, so they lead */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);
  border:1px solid var(--line);border-radius:7px;overflow:hidden;margin-top:20px}
.stat{background:var(--surface);padding:13px 16px}
.stat .k{font-family:var(--mono);font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);display:block}
.stat .v{font-family:var(--cond);font-size:30px;font-weight:700;line-height:1.05;
  font-variant-numeric:tabular-nums;display:block;margin-top:5px}
.stat .d{font-size:12px;color:var(--muted);display:block;margin-top:2px}
.stat.lead .v{color:var(--signal)}
@media(max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}}

/* pipeline — state, not a quantity, so no fake bars */
.pipe{margin-top:18px;background:var(--surface);border:1px solid var(--line);border-radius:7px;
  padding:12px 16px;display:flex;gap:10px 26px;flex-wrap:wrap;align-items:center}
.pipe .job{display:flex;align-items:center;gap:8px;font-size:13px}
.pipe .mk{font-family:var(--mono);font-size:11px}
.pipe .nm{font-weight:500}
.pipe .dt{color:var(--muted);font-size:12.5px}
.ok{color:var(--good)} .run{color:var(--signal)} .idle{color:var(--faint)}

/* filters */
.bar{position:sticky;top:0;z-index:5;margin-top:22px;background:var(--ground);
  padding-block:10px;border-bottom:1px solid var(--line);
  display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.bar input[type=search]{font-family:var(--sans);font-size:13.5px;padding:7px 11px;
  border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--ink);
  min-width:190px;flex:1 1 190px}
.bar input[type=search]:focus-visible,.chip:focus-visible,th:focus-visible{outline:2px solid var(--signal);outline-offset:1px}
.chip{font-family:var(--sans);font-size:12.5px;padding:6px 11px;border-radius:999px;cursor:pointer;
  border:1px solid var(--line);background:var(--surface);color:var(--ink-2);white-space:nowrap}
.chip[aria-pressed="true"]{background:var(--signal-soft);border-color:var(--signal);color:var(--signal-ink);font-weight:500}
.count{font-family:var(--mono);font-size:12px;color:var(--muted);margin-left:auto;
  font-variant-numeric:tabular-nums;white-space:nowrap}

/* table — the page */
.tw{margin-top:2px;overflow-x:auto;border:1px solid var(--line);border-top:0;
  border-radius:0 0 7px 7px;background:var(--surface)}
table{border-collapse:collapse;width:100%;min-width:940px}
thead th{font-family:var(--mono);font-size:10px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--faint);text-align:left;padding:10px 12px;background:var(--sunk);
  border-bottom:1px solid var(--line);position:sticky;top:52px;cursor:pointer;user-select:none;
  white-space:nowrap}
thead th[aria-sort]{color:var(--signal-ink)}
thead th .ar{font-size:9px;opacity:.75}
tbody td{padding:9px 12px;border-bottom:1px solid var(--hair);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--raised)}
.sc{font-family:var(--mono);font-weight:600;font-size:13.5px;font-variant-numeric:tabular-nums;
  display:inline-block;width:30px}
.sc-hot{color:var(--signal)} .sc-warm{color:var(--warn)} .sc-cool{color:var(--muted)}
.spark{display:inline-block;width:46px;height:4px;background:var(--hair);border-radius:2px;
  overflow:hidden;vertical-align:middle;margin-left:6px}
.spark i{display:block;height:100%;background:currentColor;opacity:.5}
td.num{font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ink-2)}
td.dim{color:var(--muted)}
td.co{font-weight:600;white-space:nowrap;max-width:170px;overflow:hidden;text-overflow:ellipsis}
td.ti{min-width:280px}
td.ti a{color:var(--ink);text-decoration:none;border-bottom:1px solid transparent}
td.ti a:hover{border-bottom-color:var(--signal)}
td.ti .loc{font-size:11.5px;color:var(--faint);margin-top:1px}
.b{font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:3px;border:1px solid currentColor}
.b-greenhouse{color:var(--gh)} .b-lever{color:var(--lv)} .b-ashby{color:var(--ab)} .b-workday{color:var(--wd)}
.stale{color:var(--crit)}
.empty{padding:40px 16px;text-align:center;color:var(--muted);font-size:13.5px}
footer{margin-top:26px;padding-top:14px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:11.5px;color:var(--faint);
  display:flex;gap:6px 18px;flex-wrap:wrap;justify-content:space-between}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <h1>Job Search Console</h1>
      <div class="sub">Every open role the pipeline is tracking, straight from company boards.</div>
    </div>
    <div class="live"><span class="dot" id="dot"></span><span id="chip">loading</span></div>
  </header>

  <section class="stats" id="stats"></section>
  <section class="pipe" id="pipe"></section>

  <div class="bar">
    <input type="search" id="q" placeholder="Search company or title" aria-label="Search company or title">
    <button class="chip" id="f-remote" aria-pressed="false">Remote</button>
    <button class="chip" id="f-strong" aria-pressed="false">70%+</button>
    <button class="chip" id="f-paid"   aria-pressed="false">Salary published</button>
    <button class="chip" id="f-fresh"  aria-pressed="false">Under 3 months</button>
    <button class="chip" id="f-new"    aria-pressed="false">Not yet reviewed</button>
    <span class="count" id="count"></span>
  </div>

  <div class="tw">
    <table>
      <thead><tr>
        <th data-k="s" aria-sort="descending">Fit <span class="ar">▼</span></th>
        <th data-k="age">Age</th>
        <th data-k="w">Where</th>
        <th data-k="emin">Exp</th>
        <th data-k="smin">Salary</th>
        <th data-k="c">Company</th>
        <th data-k="t">Role</th>
        <th data-k="b">Board</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div class="empty" id="empty" hidden>No roles match these filters.</div>

  <footer>
    <span id="foot-l"></span>
    <span id="foot-r"></span>
  </footer>
</div>

<script id="seed" type="application/json">__SEED__</script>
<script>
(() => {
  const seed = JSON.parse(document.getElementById('seed').textContent);
  let data = seed;

  const MIN=60, HR=3600, DAY=86400, WK=7*DAY, MO=30.44*DAY, YR=365.25*DAY;
  const humanize = s => {
    if (s < MIN) return 'just now';
    if (s < HR)  return Math.floor(s/MIN) + ' min';
    if (s < DAY) return Math.floor(s/HR) + ' hr';
    if (s < WK)  { const d = Math.floor(s/DAY); return d === 1 ? '1 day' : d + ' days'; }
    if (s < 5*WK){ const w = Math.floor(s/WK); return w === 1 ? '1 wk' : w + ' wks'; }
    if (s < YR)  return Math.floor(s/MO) + ' mo';
    const y = s/YR; return y >= 10 ? Math.round(y)+' yr' : y.toFixed(1)+' yr';
  };
  const ageSecs = r => r.p ? Math.max(0,(Date.now()-Date.parse(r.p))/1000) : null;
  const num = n => Number(n).toLocaleString();
  const set = (id,v) => { const e=document.getElementById(id); if(e) e.textContent=v; };
  // Salary and experience arrive as display strings; sorting needs the number.
  const firstNum = t => { const m=String(t||'').match(/(\\d[\\d,.]*)\\s*K?/); if(!m) return null;
    const v=parseFloat(m[1].replace(/,/g,'')); return /K/i.test(t)?v*1000:v; };

  let sortKey='s', sortDir=-1;
  const state = {q:'', remote:false, strong:false, paid:false, fresh:false, fresh_new:false};

  const rowsOf = () => (data.ranked || []).map(r => ({...r,
    age: ageSecs(r), emin: firstNum(r.e), smin: firstNum(r.y)}));

  function visible() {
    const q = state.q.trim().toLowerCase();
    return rowsOf().filter(r => {
      if (q && !((r.c||'')+' '+(r.t||'')).toLowerCase().includes(q)) return false;
      if (state.remote && !(r.w === 'remote' || r.w === 'hybrid')) return false;
      if (state.strong && (r.s||0) < 70) return false;
      if (state.paid && r.smin === null) return false;
      if (state.fresh && (r.age === null || r.age > 92*DAY)) return false;
      if (state.fresh_new && r.st !== 'Not Applied') return false;
      return true;
    }).sort((a,b) => {
      const A=a[sortKey], B=b[sortKey];
      if (A === null || A === undefined) return 1;      // unknowns sink, either direction
      if (B === null || B === undefined) return -1;
      if (typeof A === 'string') return sortDir * A.localeCompare(B);
      return sortDir * (A - B);
    });
  }

  function render() {
    const c = data.counters || {};
    const tiles = [
      ['Boards watched', num(c.boards ?? 0), Object.entries(c.by_board||{})
        .map(([k,v]) => k.slice(0,2)+' '+v).join(' · ') || 'direct from companies', false],
      ['Open roles', num(c.open ?? 0), 'matching your search', false],
      ['Strong fit', num(c.strong ?? 0), 'scored 70% or better', true],
      ['Not yet reviewed', num((c.by_status||{})['Not Applied'] ?? 0), 'waiting on you', false],
    ];
    document.getElementById('stats').replaceChildren(...tiles.map(([k,v,d,lead]) => {
      const el = document.createElement('div');
      el.className = 'stat' + (lead ? ' lead' : '');
      el.innerHTML = '<span class="k"></span><span class="v"></span><span class="d"></span>';
      el.querySelector('.k').textContent = k;
      el.querySelector('.v').textContent = v;
      el.querySelector('.d').textContent = d;
      return el;
    }));

    const jobs = (data.jobs || []).filter(j => j.name !== 'Jobs ranked');
    document.getElementById('pipe').replaceChildren(...jobs.map(j => {
      const done = j.total && j.done >= j.total;
      const cls = j.alive ? 'run' : (done ? 'ok' : 'idle');
      const el = document.createElement('div');
      el.className = 'job';
      el.innerHTML = '<span class="mk"></span><span class="nm"></span><span class="dt"></span>';
      el.querySelector('.mk').className = 'mk ' + cls;
      el.querySelector('.mk').textContent = j.alive ? '●' : (done ? '✓' : '○');
      el.querySelector('.nm').textContent = j.name;
      el.querySelector('.dt').textContent = j.total
        ? `${num(j.done)}/${num(j.total)} · ${j.detail}` : j.detail;
      return el;
    }));

    const live = jobs.find(j => j.alive);
    set('chip', live ? live.name.toLowerCase() + ' running' : 'all jobs idle');
    document.getElementById('dot').className = 'dot' + (live ? ' on' : '');
    if (!live) document.getElementById('dot').style.background = 'var(--faint)';

    const rows = visible();
    set('count', `${num(rows.length)} of ${num((data.ranked||[]).length)} shown`);
    document.getElementById('empty').hidden = rows.length > 0;

    const band = s => s >= 70 ? 'hot' : s >= 60 ? 'warm' : 'cool';
    const stated = v => (!v || v === 'not published') ? '' : v;
    document.getElementById('rows').replaceChildren(...rows.map(r => {
      const tr = document.createElement('tr');
      const old = r.age !== null && r.age > YR;
      tr.innerHTML = `
        <td class="num"><span class="sc sc-${band(r.s)}">${r.s}</span>
          <span class="spark sc-${band(r.s)}"><i style="width:${Math.min(100,r.s)}%"></i></span></td>
        <td class="num age"></td><td class="num dim w"></td><td class="num dim e"></td>
        <td class="num y"></td><td class="co"></td>
        <td class="ti"><a target="_blank" rel="noopener"></a><div class="loc"></div></td>
        <td><span class="b b-${r.b}"></span></td>`;
      const age = tr.querySelector('.age');
      age.textContent = r.age === null ? '' : (r.x ? '' : '~') + humanize(r.age);
      if (old) age.classList.add('stale');
      tr.querySelector('.w').textContent = stated(r.w);
      tr.querySelector('.e').textContent = stated(r.e);
      tr.querySelector('.y').textContent = stated(r.y);
      tr.querySelector('.co').textContent = r.c;
      const a = tr.querySelector('.ti a'); a.textContent = r.t; a.href = r.u || '#';
      tr.querySelector('.loc').textContent = r.l;
      tr.querySelector('.b').textContent = r.b;
      return tr;
    }));

    const when = data.updated
      ? Math.round(Math.max(0, Date.now()/1000 - data.updated)/60) : null;
    set('foot-l', when === null ? 'figures as published'
      : when < 2 ? 'updated just now' : `updated ${when} min ago`);
    set('foot-r', `${num(c.sites ?? 0)} company websites · ${num(c.careers ?? 0)} careers pages`);
  }

  document.getElementById('q').addEventListener('input', e => { state.q = e.target.value; render(); });
  [['f-remote','remote'],['f-strong','strong'],['f-paid','paid'],
   ['f-fresh','fresh'],['f-new','fresh_new']].forEach(([id,key]) => {
    const b = document.getElementById(id);
    b.addEventListener('click', () => {
      state[key] = !state[key];
      b.setAttribute('aria-pressed', String(state[key]));
      render();
    });
  });
  document.querySelectorAll('thead th').forEach(th => {
    th.tabIndex = 0;
    const go = () => {
      const k = th.dataset.k;
      if (k === sortKey) sortDir = -sortDir;
      else { sortKey = k; sortDir = (k === 'c' || k === 't' || k === 'b') ? 1 : -1; }
      document.querySelectorAll('thead th').forEach(o => {
        o.removeAttribute('aria-sort');
        const ar = o.querySelector('.ar'); if (ar) ar.remove();
      });
      th.setAttribute('aria-sort', sortDir < 0 ? 'descending' : 'ascending');
      th.insertAdjacentHTML('beforeend', ` <span class="ar">${sortDir < 0 ? '▼' : '▲'}</span>`);
      render();
    };
    th.addEventListener('click', go);
    th.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });

  render();

  // Live refresh when this file is served beside a status.json (the local
  // console does that). Opened straight from disk the fetch simply fails and
  // the baked-in snapshot stands — the page works either way, with no account
  // and no network.
  const refresh = async () => {
    try {
      const r = await fetch('status.json?t=' + Date.now(), {cache: 'no-store'});
      if (!r.ok) return;
      const fresh = await r.json();
      if (fresh && fresh.ranked) { data = fresh; render(); }
    } catch (e) { /* opened from disk: the snapshot is the data */ }
  };
  refresh();
  setInterval(refresh, 30000);
})();
</script>
"""
open(OUT, "w", encoding="utf-8").write(HTML.replace("__SEED__", json.dumps(snapshot)))
print(f"wrote {OUT} ({os.path.getsize(OUT)//1024} KB, {len(snapshot.get('ranked', []))} rows seeded)")
