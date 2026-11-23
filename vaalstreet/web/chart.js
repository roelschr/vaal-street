// A small canvas line chart: several series on one y axis, optional volume bars, hover readout.
// series: [{label, color, x:[], y:[], width, dash, dots, emphasize}]   bars: {x:[], y:[], color}
// band: {label, x:[], lo:[], hi:[], color}, a faint shaded range drawn under the lines (breaks where either side is missing)
// Colours come from the CSS tokens in style.css, so the charts follow the page's palette.
export const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function niceTicks(lo, hi, count) {
  const span = hi - lo || Math.abs(hi) || 1;
  const step0 = span / count, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

export function drawChart(host, { series, bars, band, xType = 'time', yFormat = String, tickFormat, xFormat, log = false, height }) {
  host.classList.add('chart');
  if (height) host.style.height = height + 'px';
  host.innerHTML = '<canvas></canvas><div class="tip"></div>';
  const canvas = host.firstChild, tip = host.lastChild, ctx = canvas.getContext('2d');
  const pad = { l: 8, r: 62, t: 10, b: 22 };
  const live = series.filter(s => s.y.some(v => v != null));
  const ys = live.flatMap(s => s.y.filter(v => v != null && (!log || v > 0)));
  // The band stays inside the axis, so it never runs off the top or bottom.
  if (band && ys.length) ys.push(...[...band.lo, ...band.hi].filter(v => v != null && (!log || v > 0)));
  const xs = live.flatMap(s => s.x);
  if (!ys.length) { host.innerHTML = '<div class="empty">No prints in this window.</div>'; return; }
  const tf = v => (log ? Math.log(v) : v);
  let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys.map(tf)), y1 = Math.max(...ys.map(tf));
  if (x0 === x1) x1 = x0 + 1;
  const margin = (y1 - y0 || Math.abs(y1) * 0.1 || 1) * 0.08; y0 -= margin; y1 += margin;
  const barMax = bars ? Math.max(...bars.y.map(v => v || 0), 1e-9) : 0;
  let W = 0, H = 0, hover = null;
  const px = x => pad.l + (x - x0) / (x1 - x0) * (W - pad.l - pad.r);
  const py = y => pad.t + (1 - (tf(y) - y0) / (y1 - y0)) * (H - pad.t - pad.b);
  const timeLabel = t => {
    const d = new Date(t * 1000), span = x1 - x0;
    return span > 4 * 86400 ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
      : d.toLocaleString(undefined, { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  };
  const xLabel = xFormat || (xType === 'time' ? timeLabel : v => 'day ' + v);

  function paint() {
    const dpr = window.devicePixelRatio || 1;
    W = host.clientWidth; H = host.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.font = `11px ${css('--mono')}`; ctx.textBaseline = 'middle';
    const grid = css('--line'), dim = css('--dim');
    const ticks = log ? niceTicks(y0, y1, 5).map(Math.exp) : niceTicks(y0, y1, 5);
    for (const v of ticks) {
      const y = py(v); if (y < pad.t - 1 || y > H - pad.b + 1) continue;
      ctx.strokeStyle = grid; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(pad.l, y + .5); ctx.lineTo(W - pad.r, y + .5); ctx.stroke();
      ctx.fillStyle = dim; ctx.textAlign = 'left'; ctx.fillText((tickFormat || yFormat)(v), W - pad.r + 6, y);
    }
    ctx.textAlign = 'center'; ctx.fillStyle = dim;
    const nx = Math.max(2, Math.floor((W - pad.l - pad.r) / 110));
    for (let i = 0; i <= nx; i++) {
      const v = x0 + (x1 - x0) * i / nx;
      ctx.fillText(xLabel(xType === 'time' ? v : Math.round(v)), Math.min(Math.max(px(v), pad.l + 30), W - pad.r - 30), H - 9);
    }
    if (bars) {
      const bw = Math.max(1, (W - pad.l - pad.r) / bars.x.length - 1), base = H - pad.b, top = (H - pad.t - pad.b) * 0.2;
      ctx.fillStyle = bars.color || css('--vol') + '18';
      bars.x.forEach((x, i) => { const v = bars.y[i]; if (v) ctx.fillRect(px(x) - bw / 2, base - v / barMax * top, bw, v / barMax * top); });
    }
    if (band) {
      ctx.fillStyle = band.color || css('--fair') + '22';
      const ok = i => band.lo[i] != null && band.hi[i] != null && (!log || band.lo[i] > 0);
      for (let i = 0; i < band.x.length;) {
        if (!ok(i)) { i++; continue; }
        let j = i; while (j + 1 < band.x.length && ok(j + 1)) j++;
        ctx.beginPath();
        for (let k = i; k <= j; k++) ctx.lineTo(px(band.x[k]), py(band.hi[k]));
        for (let k = j; k >= i; k--) ctx.lineTo(px(band.x[k]), py(band.lo[k]));
        ctx.closePath(); ctx.fill(); i = j + 1;
      }
    }
    for (const s of live) {
      ctx.strokeStyle = ctx.fillStyle = s.color; ctx.lineWidth = s.width || 1.5; ctx.setLineDash(s.dash || []);
      ctx.globalAlpha = s.alpha ?? 1;
      if (s.dots) {
        s.x.forEach((x, i) => { const v = s.y[i]; if (v != null && (!log || v > 0)) { ctx.beginPath(); ctx.arc(px(x), py(v), 1.6, 0, 7); ctx.fill(); } });
      } else {
        ctx.beginPath(); let pen = false;
        s.x.forEach((x, i) => {
          const v = s.y[i];
          if (v == null || (log && v <= 0)) { if (!s.bridge) pen = false; return; }
          if (pen) ctx.lineTo(px(x), py(v)); else ctx.moveTo(px(x), py(v));
          pen = true;
        });
        ctx.stroke();
      }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
    if (hover != null) {
      ctx.strokeStyle = css('--line2'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(px(hover) + .5, pad.t); ctx.lineTo(px(hover) + .5, H - pad.b); ctx.stroke();
    }
  }

  function nearest(s, x) {
    let best = -1, bd = Infinity;
    for (let i = 0; i < s.x.length; i++) { if (s.y[i] == null) continue; const d = Math.abs(s.x[i] - x); if (d < bd) { bd = d; best = i; } }
    return best;
  }
  canvas.addEventListener('mousemove', e => {
    const r = canvas.getBoundingClientRect();
    const x = x0 + (e.clientX - r.left - pad.l) / (W - pad.l - pad.r) * (x1 - x0);
    const anchor = live[0], i = nearest(anchor, x);
    if (i < 0) return;
    hover = anchor.x[i];
    const rows = live.map(s => { const j = nearest(s, hover); return j < 0 || Math.abs(s.x[j] - hover) > (x1 - x0) / 40 ? '' : `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:6px"></i>${s.label} <b>${yFormat(s.y[j])}</b></div>`; }).join('');
    const b = band ? band.x.indexOf(hover) : -1;
    const range = b >= 0 && band.lo[b] != null && band.hi[b] != null ? `<div class="muted">${band.label || 'Range'} <b>${yFormat(band.lo[b])} – ${yFormat(band.hi[b])}</b></div>` : '';
    tip.innerHTML = `<div class="muted">${xLabel(hover)}</div>${rows}${range}`;
    tip.style.display = 'block';
    const left = px(hover) + 14;
    tip.style.left = (left + tip.offsetWidth > W - 8 ? px(hover) - tip.offsetWidth - 14 : left) + 'px';
    tip.style.top = '10px';
    paint();
  });
  canvas.addEventListener('mouseleave', () => { hover = null; tip.style.display = 'none'; paint(); });
  new ResizeObserver(paint).observe(host);
  paint();
}

export function spark(values, color) {
  const v = (values || []).filter(x => x != null);
  if (v.length < 2) return '';
  const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  const pts = v.map((y, i) => `${(i / (v.length - 1) * 82 + 1).toFixed(1)},${(20 - (y - lo) / span * 18).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 84 22"><polyline fill="none" style="stroke:${color}" stroke-width="1.4" points="${pts}"/></svg>`;
}
