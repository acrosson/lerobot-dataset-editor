/* Canvas line charts for joint trajectories.
 *
 * The tracks scroll horizontally as one unit, so a fixed y-axis gutter would
 * either scroll away or cover data. Instead each chart states its own range in
 * its sticky label and the crosshair reports exact values — the axis a reviewer
 * actually reads here is time, which the ruler carries. */
const Chart = (() => {
  const GRID = 'rgba(255,255,255,.055)';
  const ZERO = 'rgba(255,255,255,.16)';
  const TEXT = '#8b8a82';

  function fit(canvas, width, height) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.round(width * dpr));
    canvas.height = Math.max(1, Math.round(height * dpr));
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return ctx;
  }

  function niceStep(span, target) {
    const raw = span / Math.max(1, target);
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const norm = raw / mag;
    const step = norm >= 5 ? 10 : norm >= 2 ? 5 : norm >= 1 ? 2 : 1;
    return step * mag;
  }

  function domain(series) {
    let lo = Infinity, hi = -Infinity;
    for (const s of series) {
      if (!s.visible || !s.data) continue;
      for (const v of s.data) {
        if (!Number.isFinite(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (!Number.isFinite(lo)) return [0, 1];
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.08;
    return [lo - pad, hi + pad];
  }

  /* One multi-series line chart across `frames` frames at `px` px/frame. */
  function line(canvas, opts) {
    const { series, frames, px, height, padTop = 14, padBottom = 6 } = opts;
    const width = Math.max(1, frames * px);
    const ctx = fit(canvas, width, height);
    const plotTop = padTop, plotH = Math.max(1, height - padTop - padBottom);
    const [lo, hi] = opts.domain || domain(series);
    const y = v => plotTop + plotH - ((v - lo) / (hi - lo)) * plotH;

    // recessive horizontal grid, with zero called out when it's in range
    const step = niceStep(hi - lo, 4);
    ctx.lineWidth = 1;
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const yy = Math.round(y(v)) + 0.5;
      ctx.strokeStyle = Math.abs(v) < step / 1e6 ? ZERO : GRID;
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(width, yy); ctx.stroke();
    }

    // one pass per series; sub-pixel dense data is decimated to min/max columns
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    for (const s of series) {
      if (!s.visible || !s.data || !s.data.length) continue;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 2;
      ctx.globalAlpha = s.alpha || 1;
      if (s.dash) ctx.setLineDash(s.dash); else ctx.setLineDash([]);
      ctx.beginPath();
      if (px >= 1) {
        for (let i = 0; i < s.data.length; i++) {
          const xx = i * px + px / 2;
          if (i === 0) ctx.moveTo(xx, y(s.data[i])); else ctx.lineTo(xx, y(s.data[i]));
        }
      } else {
        const cols = Math.max(1, Math.round(width));
        const per = s.data.length / cols;
        for (let c = 0; c < cols; c++) {
          const a = Math.floor(c * per), b = Math.min(s.data.length, Math.max(a + 1, Math.floor((c + 1) * per)));
          let mn = Infinity, mx = -Infinity;
          for (let i = a; i < b; i++) { const v = s.data[i]; if (v < mn) mn = v; if (v > mx) mx = v; }
          if (!Number.isFinite(mn)) continue;
          if (c === 0) ctx.moveTo(c + 0.5, y(mn));
          ctx.lineTo(c + 0.5, y(mx));
          ctx.lineTo(c + 0.5, y(mn));
        }
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
    return [lo, hi];
  }

  /* Filled area from the baseline — used for the motion trace, where the shape
     that matters is "how much of this is flat". */
  function area(canvas, opts) {
    const { data, frames, px, height, color, padTop = 14, padBottom = 6 } = opts;
    const width = Math.max(1, frames * px);
    const ctx = fit(canvas, width, height);
    const plotTop = padTop, plotH = Math.max(1, height - padTop - padBottom);
    const hi = Math.max(1e-6, opts.max ?? Math.max(...data));
    const y = v => plotTop + plotH - Math.min(1, v / hi) * plotH;

    ctx.strokeStyle = GRID; ctx.lineWidth = 1;
    const base = Math.round(plotTop + plotH) + 0.5;
    ctx.beginPath(); ctx.moveTo(0, base); ctx.lineTo(width, base); ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(0, base);
    const cols = px >= 1 ? data.length : Math.max(1, Math.round(width));
    for (let c = 0; c < cols; c++) {
      let v;
      if (px >= 1) { v = data[c]; }
      else {
        const per = data.length / cols;
        const a = Math.floor(c * per), b = Math.min(data.length, Math.max(a + 1, Math.floor((c + 1) * per)));
        v = 0; for (let i = a; i < b; i++) v = Math.max(v, data[i]);
      }
      const xx = px >= 1 ? c * px + px / 2 : c + 0.5;
      ctx.lineTo(xx, y(v));
    }
    ctx.lineTo(width, base);
    ctx.closePath();
    ctx.fillStyle = color + '55';
    ctx.fill();
    ctx.strokeStyle = color; ctx.lineWidth = 1.5;
    ctx.stroke();
    return [0, hi];
  }

  /* Time ruler: seconds where they fit, frame counts when zoomed right in. */
  function ruler(canvas, opts) {
    const { frames, px, fps, height = 18 } = opts;
    const width = Math.max(1, frames * px);
    const ctx = fit(canvas, width, height);
    ctx.font = '10px ui-monospace, Menlo, monospace';
    ctx.textBaseline = 'top';

    const minGap = 56;
    let stepFrames = fps;
    while (stepFrames * px < minGap) stepFrames *= 2;
    if (px * fps > minGap * 4) {
      stepFrames = fps;
      while (stepFrames > 1 && (stepFrames / 2) * px >= minGap) stepFrames = Math.round(stepFrames / 2);
    }
    for (let f = 0; f <= frames; f += stepFrames) {
      const xx = Math.round(f * px) + 0.5;
      ctx.strokeStyle = GRID;
      ctx.beginPath(); ctx.moveTo(xx, 0); ctx.lineTo(xx, height); ctx.stroke();
      ctx.fillStyle = TEXT;
      const secs = f / fps;
      const label = stepFrames >= fps ? `${secs.toFixed(secs % 1 ? 1 : 0)}s` : `${secs.toFixed(2)}s`;
      ctx.fillText(label, xx + 3, 3);
    }
    return stepFrames;
  }

  return { line, area, ruler, fit, domain };
})();
