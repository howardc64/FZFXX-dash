#!/usr/bin/env python3
"""
generate_fzfxx_dashboard.py
===========================
Reads a Fidelity account CSV (the kind exported from Fidelity.com) and
produces a self-contained HTML dashboard for the FZFXX core-account balance.

Usage
-----
    python generate_fzfxx_dashboard.py account_fzfxx.csv
    python generate_fzfxx_dashboard.py account_fzfxx.csv -o my_dashboard.html

The output file is written to the same directory as the input CSV unless
-o / --output is given.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ── CSV parsing ───────────────────────────────────────────────────────────────

def parse_amount(s: str) -> float | None:
    """Convert '$1,234.56' or '-28703' or '' to float; None if blank."""
    if not s or not s.strip():
        return None
    cleaned = s.strip().replace(",", "").replace("$", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(s: str) -> str | None:
    """MM/DD/YYYY → YYYY-MM-DD.  Returns None if unparseable."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def load_csv(path: str) -> list[dict]:
    """
    Skip the two blank header rows Fidelity prepends, find the real header
    row, and return a list of row-dicts.  Also skip the legal-boilerplate
    footer rows at the bottom.
    """
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        raw = list(csv.reader(f))

    # Find the header row (starts with "Run Date")
    header_idx = None
    for i, row in enumerate(raw):
        if row and row[0].strip() == "Run Date":
            header_idx = i
            break

    if header_idx is None:
        sys.exit("ERROR: Could not find 'Run Date' header row in the CSV.")

    headers = [h.strip() for h in raw[header_idx]]

    for row in raw[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        # Skip footer boilerplate (starts with a long sentence)
        if len(row[0]) > 40 and not re.match(r"\d{2}/\d{2}/\d{4}", row[0]):
            continue
        if not re.match(r"\d{2}/\d{2}/\d{4}", row[0].strip()):
            continue
        record = {headers[i]: row[i].strip() if i < len(row) else "" for i in range(len(headers))}
        rows.append(record)

    return rows


# ── Transaction classification ────────────────────────────────────────────────

# Description patterns → (category, is_deposit)
WITHDRAWAL_PATTERNS = [
    (re.compile(r"IRS USATAXPYMT", re.I),           "IRS Tax Payment"),
    (re.compile(r"ADVISOR FEE",     re.I),           "Advisor Fee"),
    (re.compile(r"TRANSFERRED TO",  re.I),           "Transfer Out"),
    (re.compile(r"Electronic Funds Transfer Paid", re.I), "Monthly Transfer"),
    (re.compile(r"PERSONAL WITHDRAWAL.*FZFXX",  re.I), None),  # internal – skip
    (re.compile(r"REDEMPTION FROM CORE.*FZFXX",re.I), None),  # internal – skip
]

DEPOSIT_PATTERNS = [
    (re.compile(r"BLUEPRINT CAPITAL INCOME REIT",  re.I), "Blueprint REIT"),
    (re.compile(r"DFA|DFEMX|DFQTX|DFVQX|DFREX",  re.I), "Fund Dividends"),
    (re.compile(r"VANGUARD TOTAL|VTI\b",            re.I), "Fund Dividends"),
    (re.compile(r"VANGUARD (LIMITD|INTERMD|DEVELOPED)", re.I), "Bond Dividends"),
    (re.compile(r"VTMGX",                           re.I), "Bond Dividends"),
    (re.compile(r"YOU SOLD.*VMLUX",                re.I), "Asset Sale"),
    (re.compile(r"YOU SOLD.*VANGUARD LIMITD",      re.I), "Asset Sale"),
    (re.compile(r"YOU BOUGHT.*FZFXX",              re.I), None),  # internal – skip
    (re.compile(r"REINVESTMENT CASH",              re.I), None),  # tiny – skip
    (re.compile(r"INTEREST EARNED",               re.I), None),  # tiny – skip
    (re.compile(r"DIVIDEND.*FZFXX",               re.I), None),  # tiny – skip
]

# Minimum absolute amount to be listed as a "key" event
WITHDRAWAL_THRESHOLD = 3500
DEPOSIT_THRESHOLD    = 600


def classify_row(row: dict):
    """
    Return (category, is_deposit) or (None, None) if the row should be skipped.
    """
    desc   = row.get("Description", "") + " " + row.get("Action", "")
    amount = parse_amount(row.get("Amount ($)", ""))
    if amount is None:
        return None, None

    if amount < 0:  # withdrawal
        for pattern, category in WITHDRAWAL_PATTERNS:
            if pattern.search(desc):
                if category is None:
                    return None, None
                if abs(amount) >= WITHDRAWAL_THRESHOLD:
                    return category, False
                return None, None
        return None, None

    else:  # deposit / inflow
        for pattern, category in DEPOSIT_PATTERNS:
            if pattern.search(desc):
                if category is None:
                    return None, None
                if amount >= DEPOSIT_THRESHOLD:
                    return category, True
                return None, None
        return None, None


# ── Build data structures ─────────────────────────────────────────────────────

def build_data(rows: list[dict]):
    """Return (bal_series, withdrawals, deposits)."""

    # Balance series: one point per date (last FZFXX Balance seen on that date)
    bal_by_date: dict[str, float] = {}
    withdrawals: list[dict] = []
    deposits:    list[dict] = []

    for row in rows:
        date_raw = row.get("Run Date", "")
        date     = parse_date(date_raw)
        if not date:
            continue

        # Balance
        bal_raw = row.get("FZFXX Balance ($)", "")
        bal     = parse_amount(bal_raw)
        if bal is not None:
            bal_by_date[date] = bal

        # Events
        category, is_deposit = classify_row(row)
        amount = parse_amount(row.get("Amount ($)", ""))
        if category and amount is not None:
            entry = {"date": date, "amount": abs(amount), "category": category}
            if is_deposit:
                deposits.append(entry)
            else:
                withdrawals.append(entry)

    # Sort balance series chronologically
    bal_series = sorted(
        [[d, v] for d, v in bal_by_date.items()],
        key=lambda x: x[0]
    )

    return bal_series, withdrawals, deposits


def build_summaries(events: list[dict], years: list[str]) -> dict:
    """{ category: { year: total, ... }, ... }"""
    summary: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for e in events:
        yr = e["date"][:4]
        if yr in years:
            summary[e["category"]][yr] += e["amount"]
    return {cat: dict(by_yr) for cat, by_yr in summary.items()}


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FZFXX Balance Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=DM+Serif+Display&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0d1117; --surface:#161b22; --border:#21262d;
    --text:#e6edf3; --muted:#7d8590; --accent:#58a6ff;
  }}
  *{{ box-sizing:border-box; margin:0; padding:0; }}
  body{{ background:var(--bg); color:var(--text); font-family:'IBM Plex Mono',monospace; font-size:13px; line-height:1.6; padding:24px; }}
  h1{{ font-family:'DM Serif Display',serif; font-size:28px; color:var(--text); letter-spacing:.5px; margin-bottom:4px; }}
  .subtitle{{ color:var(--muted); font-size:11px; margin-bottom:20px; letter-spacing:.5px; }}
  .section-title{{ font-size:11px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px; }}
  .card{{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:20px; margin-bottom:20px; }}
  .controls{{ display:flex; gap:24px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }}
  .cb-label{{ display:flex; align-items:center; gap:8px; cursor:pointer; font-size:12px; user-select:none; }}
  .cb-label input[type=checkbox]{{ display:none; }}
  .cb-box{{ width:16px; height:16px; border-radius:4px; border:2px solid; display:flex; align-items:center; justify-content:center; flex-shrink:0; transition:background .15s; position:relative; }}
  .cb-check{{ position:absolute; font-size:11px; font-weight:700; opacity:0; transition:opacity .15s; }}
  input[type=checkbox]:checked ~ .cb-box .cb-check{{ opacity:1; }}
  .legend{{ display:flex; flex-wrap:wrap; gap:14px; margin-top:14px; }}
  .legend-item{{ display:flex; align-items:center; gap:6px; font-size:11px; color:var(--muted); }}
  table{{ width:100%; border-collapse:collapse; font-size:12px; }}
  th{{ text-align:left; color:var(--muted); font-weight:500; padding:6px 10px; border-bottom:1px solid var(--border); font-size:11px; letter-spacing:.5px; }}
  td{{ padding:7px 10px; border-bottom:1px solid rgba(33,38,45,.5); color:var(--text); }}
  tr:last-child td{{ border-bottom:none; }}
  tr:hover td{{ background:rgba(88,166,255,.04); }}
  .num{{ text-align:right; font-variant-numeric:tabular-nums; }}
  .pos{{ color:#4ade80; }} .neg{{ color:#f87171; }}
  .total-row td{{ color:var(--accent); font-weight:600; border-top:1px solid var(--border); }}
  .cat-badge{{ display:inline-block; padding:1px 7px; border-radius:10px; font-size:10px; font-weight:600; }}
  #tooltip{{
    position:fixed; background:#1c2128; border:1px solid var(--border); border-radius:6px;
    padding:10px 14px; font-size:12px; pointer-events:none; display:none; z-index:100;
    box-shadow:0 8px 24px rgba(0,0,0,.5); min-width:200px; max-width:280px;
  }}
  #tooltip .tt-date{{ color:var(--muted); font-size:11px; margin-bottom:4px; }}
  #tooltip .tt-val{{ color:var(--accent); font-weight:600; font-size:15px; margin-bottom:4px; }}
  #tooltip .tt-evt{{ font-size:11px; line-height:1.8; }}
  .two-col{{ display:grid; grid-template-columns:1fr 1fr; gap:20px; }}
  @media(max-width:700px){{ .two-col{{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>

<h1>FZFXX Balance</h1>
<div class="subtitle">Fidelity Treasury Money Market &middot; {subtitle}</div>

<div class="card">
  <div class="section-title">Balance Over Time</div>
  <div class="controls">
    <label class="cb-label">
      <input type="checkbox" id="cb-w" checked>
      <div class="cb-box" style="border-color:#f87171;color:#f87171"><span class="cb-check">&#10003;</span></div>
      <span>Show Withdrawals</span>
    </label>
    <label class="cb-label">
      <input type="checkbox" id="cb-d" checked>
      <div class="cb-box" style="border-color:#4ade80;color:#4ade80"><span class="cb-check">&#10003;</span></div>
      <span>Show Deposits</span>
    </label>
  </div>
  <div style="position:relative;width:100%">
    <svg id="chart" style="width:100%;display:block;overflow:visible"></svg>
  </div>
  <div class="legend" id="legend"></div>
</div>

<div class="two-col">
  <div class="card"><div class="section-title">Key Withdrawals</div><table id="w-table"></table></div>
  <div class="card"><div class="section-title">Key Deposits</div><table id="d-table"></table></div>
</div>
<div class="two-col">
  <div class="card"><div class="section-title">Withdrawal Summary by Year</div><table id="ws-table"></table></div>
  <div class="card"><div class="section-title">Deposit Summary by Year</div><table id="ds-table"></table></div>
</div>

<div id="tooltip">
  <div class="tt-date" id="tt-date"></div>
  <div class="tt-val"  id="tt-val"></div>
  <div class="tt-evt"  id="tt-evt"></div>
</div>

<script>
const BAL         = {bal_json};
const WITHDRAWALS = {withdrawals_json};
const DEPOSITS    = {deposits_json};
const W_COLORS    = {w_colors_json};
const D_COLORS    = {d_colors_json};
const W_SUMMARY   = {w_summary_json};
const D_SUMMARY   = {d_summary_json};
const W_YEARS     = {w_years_json};
const D_YEARS     = {d_years_json};

// ── helpers ───────────────────────────────────────────────────────────────────
const NS    = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs) => {{
  const e = document.createElementNS(NS, tag);
  for (const [k,v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}};
const parseDate = s => new Date(s + 'T00:00:00');
const fmt  = v => '$' + Math.abs(v).toLocaleString('en-US', {{minimumFractionDigits:0, maximumFractionDigits:0}});
const fmtD = d => {{ const p = d.split('-'); return `${{p[1]}}/${{p[2]}}/${{p[0]}}`; }};

function groupByDate(arr) {{
  const m = {{}};
  for (const item of arr) {{ if (!m[item.date]) m[item.date] = []; m[item.date].push(item); }}
  return m;
}}
const wByDate = groupByDate(WITHDRAWALS);
const dByDate = groupByDate(DEPOSITS);

// ── chart ─────────────────────────────────────────────────────────────────────
function drawChart() {{
  const svg = document.getElementById('chart');
  const W   = svg.parentElement.clientWidth || 800;
  const H   = 400;
  const PAD = {{top:20, right:24, bottom:54, left:82}};
  svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
  svg.setAttribute('height', H);
  svg.innerHTML = '';

  const showW = document.getElementById('cb-w').checked;
  const showD = document.getElementById('cb-d').checked;

  const maxVal = Math.max(...BAL.map(b => b[1])) * 1.12;
  const minVal = 0;
  const chartH = H - PAD.top - PAD.bottom;
  const chartBottom = H - PAD.bottom;

  const t0 = parseDate(BAL[0][0]).getTime();
  const t1 = parseDate(BAL[BAL.length-1][0]).getTime();
  const cx  = t => PAD.left + (t - t0) / (t1 - t0) * (W - PAD.left - PAD.right);
  const cy  = v => PAD.top  + (1 - (Math.max(v,0) - minVal) / (maxVal - minVal)) * chartH;
  const dH  = amt => Math.min((amt / (maxVal - minVal)) * chartH, chartH);

  // gradient def
  const defs = svgEl('defs', {{}});
  const grad = svgEl('linearGradient', {{id:'balg', x1:'0', y1:'0', x2:'0', y2:'1'}});
  grad.appendChild(svgEl('stop', {{'offset':'0%',   'stop-color':'#58a6ff', 'stop-opacity':'0.22'}}));
  grad.appendChild(svgEl('stop', {{'offset':'100%', 'stop-color':'#58a6ff', 'stop-opacity':'0.02'}}));
  defs.appendChild(grad);
  svg.appendChild(defs);

  // Y grid + labels
  for (let i = 0; i <= 6; i++) {{
    const v = minVal + (maxVal - minVal) * i / 6, y = cy(v);
    svg.appendChild(svgEl('line', {{x1:PAD.left, y1:y, x2:W-PAD.right, y2:y, stroke:'#21262d', 'stroke-width':'1'}}));
    const t = svgEl('text', {{x:PAD.left-8, y:y+4, 'text-anchor':'end', fill:'#7d8590', 'font-size':'10', 'font-family':'IBM Plex Mono'}});
    t.textContent = fmt(v);
    svg.appendChild(t);
  }}

  // X axis
  svg.appendChild(svgEl('line', {{x1:PAD.left, y1:chartBottom, x2:W-PAD.right, y2:chartBottom, stroke:'#444d56', 'stroke-width':'1.5'}}));

  // Auto-generate quarter + mid-quarter labels spanning the data range
  const startYear = parseInt(BAL[0][0].slice(0,4));
  const endYear   = parseInt(BAL[BAL.length-1][0].slice(0,4));
  const quarterLabels = [], halfLabels = [];
  for (let y = startYear; y <= endYear + 1; y++) {{
    for (let q = 0; q < 4; q++) {{
      const mo = q * 3 + 1;
      const d  = `${{y}}-${{String(mo).padStart(2,'0')}}-01`;
      const qn = `Q${{q+1}}'${{String(y).slice(2)}}`;
      quarterLabels.push({{l:qn, d}});
      // mid-quarter (month 2 of each quarter)
      const mo2 = mo + 1;
      const d2  = `${{y}}-${{String(mo2).padStart(2,'0')}}-01`;
      const mo3 = mo + 2;
      const d3  = `${{y}}-${{String(mo3).padStart(2,'0')}}-01`;
      const monthNames = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      halfLabels.push({{l:monthNames[mo2]+`'${{String(y).slice(2)}}`, d:d2}});
      halfLabels.push({{l:monthNames[mo3]+`'${{String(y).slice(2)}}`, d:d3}});
    }}
  }}

  for (const q of quarterLabels) {{
    const t = parseDate(q.d).getTime();
    if (t < t0 - 86400000*20 || t > t1 + 86400000*20) continue;
    const x = cx(t);
    svg.appendChild(svgEl('line', {{x1:x, y1:PAD.top, x2:x, y2:chartBottom+6, stroke:'#444d56', 'stroke-width':'1.5'}}));
    const lbl = svgEl('text', {{x:x, y:chartBottom+20, 'text-anchor':'middle', fill:'#e6edf3', 'font-size':'11', 'font-family':'IBM Plex Mono', 'font-weight':'600'}});
    lbl.textContent = q.l;
    svg.appendChild(lbl);
  }}
  for (const q of halfLabels) {{
    const t = parseDate(q.d).getTime();
    if (t < t0 || t > t1) continue;
    const x = cx(t);
    svg.appendChild(svgEl('line', {{x1:x, y1:chartBottom, x2:x, y2:chartBottom+4, stroke:'#444d56', 'stroke-width':'1', 'stroke-dasharray':'2 3'}}));
    const lbl = svgEl('text', {{x:x, y:chartBottom+34, 'text-anchor':'middle', fill:'#6e7681', 'font-size':'9', 'font-family':'IBM Plex Mono'}});
    lbl.textContent = q.l;
    svg.appendChild(lbl);
  }}

  // Balance area fill
  let area = `M ${{cx(t0)}} ${{chartBottom}} L ${{cx(parseDate(BAL[0][0]).getTime())}} ${{cy(BAL[0][1])}}`;
  for (let i = 1; i < BAL.length; i++) {{
    const xc = cx(parseDate(BAL[i][0]).getTime());
    area += ` L ${{xc}} ${{cy(BAL[i-1][1])}} L ${{xc}} ${{cy(BAL[i][1])}}`;
  }}
  area += ` L ${{cx(t1)}} ${{chartBottom}} Z`;
  svg.appendChild(svgEl('path', {{d:area, fill:'url(#balg)'}}));

  // Balance step line
  let line = `M ${{cx(parseDate(BAL[0][0]).getTime())}} ${{cy(BAL[0][1])}}`;
  for (let i = 1; i < BAL.length; i++) {{
    const xc = cx(parseDate(BAL[i][0]).getTime());
    line += ` L ${{xc}} ${{cy(BAL[i-1][1])}} L ${{xc}} ${{cy(BAL[i][1])}}`;
  }}
  svg.appendChild(svgEl('path', {{d:line, fill:'none', stroke:'#58a6ff', 'stroke-width':'2'}}));

  // Deposit lines (dashed, green, top layer)
  if (showD) {{
    const dG = svgEl('g', {{'data-group':'dep'}});
    for (const [date, ds] of Object.entries(dByDate)) {{
      const t = parseDate(date).getTime();
      if (t < t0 || t > t1) continue;
      const x    = cx(t);
      const cats = {{}};
      for (const d of ds) cats[d.category] = (cats[d.category] || 0) + d.amount;
      let offset = 0;
      for (const [cat, amt] of Object.entries(cats)) {{
        const color = D_COLORS[cat] || '#4ade80';
        const h = dH(amt);
        dG.appendChild(svgEl('line', {{x1:x, y1:chartBottom-offset-h, x2:x, y2:chartBottom-offset, stroke:color, 'stroke-width':'2.5', 'stroke-dasharray':'5 3', opacity:'0.95'}}));
        offset += h;
      }}
      const totalH   = dH(ds.reduce((s,d) => s+d.amount, 0));
      const topColor = D_COLORS[Object.keys(cats)[0]] || '#4ade80';
      dG.appendChild(svgEl('polygon', {{points:`${{x-4}},${{chartBottom-totalH+9}} ${{x+4}},${{chartBottom-totalH+9}} ${{x}},${{chartBottom-totalH}}`, fill:topColor, opacity:'1'}}));
    }}
    svg.appendChild(dG);
  }}

  // Withdrawal lines (solid, red, top layer)
  if (showW) {{
    const wG = svgEl('g', {{'data-group':'wdr'}});
    for (const [date, ws] of Object.entries(wByDate)) {{
      const t = parseDate(date).getTime();
      if (t < t0 || t > t1) continue;
      const x    = cx(t);
      const cats = {{}};
      for (const w of ws) cats[w.category] = (cats[w.category] || 0) + w.amount;
      let offset = 0;
      for (const [cat, amt] of Object.entries(cats)) {{
        const color = W_COLORS[cat] || '#f87171';
        const h = dH(amt);
        wG.appendChild(svgEl('line', {{x1:x, y1:chartBottom-offset-h, x2:x, y2:chartBottom-offset, stroke:color, 'stroke-width':'2.5', opacity:'0.95'}}));
        offset += h;
      }}
      const totalH  = dH(ws.reduce((s,w) => s+w.amount, 0));
      const topCat  = Object.entries(cats).sort((a,b) => b[1]-a[1])[0][0];
      wG.appendChild(svgEl('polygon', {{points:`${{x-4}},${{chartBottom-totalH+9}} ${{x+4}},${{chartBottom-totalH+9}} ${{x}},${{chartBottom-totalH}}`, fill:W_COLORS[topCat]||'#f87171', opacity:'1'}}));
    }}
    svg.appendChild(wG);
  }}

  // Hover rects
  const hG = svgEl('g', {{}});
  for (let i = 0; i < BAL.length; i++) {{
    const t  = parseDate(BAL[i][0]).getTime();
    const tE = i < BAL.length-1 ? parseDate(BAL[i+1][0]).getTime() : t1;
    const x1 = cx(t), x2 = cx(tE);
    const r  = svgEl('rect', {{x:x1, y:PAD.top, width:Math.max(3, x2-x1), height:chartH, fill:'transparent', cursor:'crosshair'}});
    const bv = BAL[i][1], bd = BAL[i][0];
    r.addEventListener('mousemove', e => showTip(e, bd, bv));
    r.addEventListener('mouseleave', () => {{ document.getElementById('tooltip').style.display='none'; }});
    hG.appendChild(r);
  }}
  svg.appendChild(hG);
}}

function showTip(e, bd, bv) {{
  const evts = [];
  if (wByDate[bd]) evts.push(...wByDate[bd].map(w => `<span style="color:${{W_COLORS[w.category]||'#f87171'}}">&#8595; ${{w.category}}: ${{fmt(w.amount)}}</span>`));
  if (dByDate[bd]) evts.push(...dByDate[bd].map(d => `<span style="color:${{D_COLORS[d.category]||'#4ade80'}}">&#8593; ${{d.category}}: ${{fmt(d.amount)}}</span>`));
  document.getElementById('tt-date').textContent = fmtD(bd);
  document.getElementById('tt-val').textContent  = fmt(bv);
  document.getElementById('tt-evt').innerHTML    = evts.join('<br>');
  const tt  = document.getElementById('tooltip');
  tt.style.display = 'block';
  const ttW = 220, ttH = 110;
  let lx = e.clientX + 16, ly = e.clientY - 40;
  if (lx + ttW > window.innerWidth  - 8) lx = e.clientX - ttW - 8;
  if (ly + ttH > window.innerHeight - 8) ly = e.clientY - ttH - 8;
  if (ly < 8) ly = 8;
  tt.style.left = lx + 'px';
  tt.style.top  = ly + 'px';
}}

// ── legend ────────────────────────────────────────────────────────────────────
function buildLegend() {{
  const legend = document.getElementById('legend');
  const items = [
    {{label:'FZFXX Balance', color:'#58a6ff', dash:false, thick:2}},
    ...Object.entries(W_COLORS).map(([k,v]) => ({{label:k, color:v, dash:false, thick:3}})),
    ...Object.entries(D_COLORS).map(([k,v]) => ({{label:k, color:v, dash:true,  thick:3}})),
  ];
  for (const item of items) {{
    const div = document.createElement('div'); div.className = 'legend-item';
    const s   = document.createElementNS(NS, 'svg'); s.setAttribute('width','28'); s.setAttribute('height','14');
    const ln  = document.createElementNS(NS, 'line');
    ln.setAttribute('x1','0'); ln.setAttribute('y1','7'); ln.setAttribute('x2','28'); ln.setAttribute('y2','7');
    ln.setAttribute('stroke', item.color); ln.setAttribute('stroke-width', item.thick||2);
    if (item.dash) ln.setAttribute('stroke-dasharray','5 3');
    s.appendChild(ln); div.appendChild(s);
    const sp = document.createElement('span'); sp.textContent = item.label; div.appendChild(sp);
    legend.appendChild(div);
  }}
}}

// ── tables ────────────────────────────────────────────────────────────────────
function buildEventTable(id, items, colors, isDeposit) {{
  const tbl    = document.getElementById(id);
  const sorted = [...items].sort((a,b) => b.amount - a.amount);
  let html = `<tr><th>Date</th><th>Category</th><th class="num">Amount</th></tr>`;
  for (const item of sorted) {{
    const color = colors[item.category] || (isDeposit ? '#4ade80' : '#f87171');
    html += `<tr><td>${{fmtD(item.date)}}</td><td><span class="cat-badge" style="background:${{color}}22;color:${{color}}">${{item.category}}</span></td><td class="num ${{isDeposit?'pos':'neg'}}">${{fmt(item.amount)}}</td></tr>`;
  }}
  html += `<tr class="total-row"><td colspan="2">Total</td><td class="num">${{fmt(items.reduce((s,i)=>s+i.amount,0))}}</td></tr>`;
  tbl.innerHTML = html;
}}

function buildSummaryTable(id, summary, years, colors, isDeposit) {{
  const tbl  = document.getElementById(id);
  let html   = `<tr><th>Category</th>${{years.map(y=>`<th class="num">${{y}}</th>`).join('')}}<th class="num">Total</th></tr>`;
  const gTot = {{}};
  for (const [cat, byYear] of Object.entries(summary)) {{
    const color = colors[cat] || (isDeposit ? '#4ade80' : '#f87171');
    const total = years.reduce((s,y) => s + (byYear[y]||0), 0);
    years.forEach(y => {{ gTot[y] = (gTot[y]||0) + (byYear[y]||0); }});
    html += `<tr><td><span class="cat-badge" style="background:${{color}}22;color:${{color}}">${{cat}}</span></td>${{years.map(y=>`<td class="num ${{isDeposit?'pos':'neg'}}">${{byYear[y]?fmt(byYear[y]):'-'}}</td>`).join('')}}<td class="num">${{fmt(total)}}</td></tr>`;
  }}
  html += `<tr class="total-row"><td>Total</td>${{years.map(y=>`<td class="num">${{fmt(gTot[y]||0)}}</td>`).join('')}}<td class="num">${{fmt(years.reduce((s,y)=>s+(gTot[y]||0),0))}}</td></tr>`;
  tbl.innerHTML = html;
}}

// ── init ──────────────────────────────────────────────────────────────────────
buildLegend();
buildEventTable('w-table', WITHDRAWALS, W_COLORS, false);
buildEventTable('d-table', DEPOSITS,    D_COLORS, true);
buildSummaryTable('ws-table', W_SUMMARY, W_YEARS, W_COLORS, false);
buildSummaryTable('ds-table', D_SUMMARY, D_YEARS, D_COLORS, true);
drawChart();

document.getElementById('cb-w').addEventListener('change', drawChart);
document.getElementById('cb-d').addEventListener('change', drawChart);
window.addEventListener('resize', drawChart);
</script>
</body>
</html>
"""


# ── Color palettes (exported to JS) ──────────────────────────────────────────

W_COLORS = {
    "IRS Tax Payment":  "#dc2626",
    "Advisor Fee":      "#f97316",
    "Transfer Out":     "#fb7185",
    "Monthly Transfer": "#fca5a5",
}

D_COLORS = {
    "Fund Dividends": "#16a34a",
    "Asset Sale":     "#65a30d",
    "Blueprint REIT": "#0d9488",
    "Bond Dividends": "#6ee7b7",
}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate FZFXX HTML dashboard from Fidelity CSV.")
    parser.add_argument("csv_file", help="Path to the exported Fidelity account CSV")
    parser.add_argument("-o", "--output", help="Output HTML file path (default: <csv_file>.html)")
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: File not found: {csv_path}")

    output_path = Path(args.output) if args.output else csv_path.with_suffix(".html")

    print(f"Reading  {csv_path}")
    rows = load_csv(str(csv_path))
    print(f"  Parsed {len(rows)} data rows")

    bal_series, withdrawals, deposits = build_data(rows)
    print(f"  Balance points : {len(bal_series)}")
    print(f"  Key withdrawals: {len(withdrawals)}")
    print(f"  Key deposits   : {len(deposits)}")

    if not bal_series:
        sys.exit("ERROR: No FZFXX balance data found. Check the 'FZFXX Balance ($)' column.")

    # Date range subtitle
    start_date = datetime.strptime(bal_series[0][0],  "%Y-%m-%d").strftime("%b %Y")
    end_date   = datetime.strptime(bal_series[-1][0], "%Y-%m-%d").strftime("%b %Y")
    subtitle   = f"{start_date} \u2013 {end_date}"

    # Years present in data
    all_years = sorted({e["date"][:4] for e in withdrawals + deposits})
    if not all_years:
        all_years = sorted({d[:4] for d, _ in bal_series})

    w_summary = build_summaries(withdrawals, all_years)
    d_summary = build_summaries(deposits,    all_years)

    html = HTML_TEMPLATE.format(
        subtitle        = subtitle,
        bal_json        = json.dumps(bal_series),
        withdrawals_json= json.dumps(withdrawals),
        deposits_json   = json.dumps(deposits),
        w_colors_json   = json.dumps(W_COLORS),
        d_colors_json   = json.dumps(D_COLORS),
        w_summary_json  = json.dumps(w_summary),
        d_summary_json  = json.dumps(d_summary),
        w_years_json    = json.dumps(all_years),
        d_years_json    = json.dumps(all_years),
    )

    output_path.write_text(html, encoding="utf-8")
    print(f"Written  {output_path}")


if __name__ == "__main__":
    main()
