// Text-zone check over every frame (step 1/30 s).
// Usage (from the repo root): node ads/video/tools/textcheck.js ads/video/channel/<slug>.html [from] [to]
// Letter boxes: Range.getClientRects() of every text node + window.TEXTBOX (text drawn on canvas).
// Only the readable phase counts: effective opacity (product over ancestors) >= 0.5, blur < 4 px
// (CSS blur() + stdDeviation of url(#…) blur filters), t outside the CUES "free" windows.
// Zones (STYLE 6.5): x 90..990, y 440..1420; below y 900 x1 <= 940 and a centred line <= 800 px;
// above y 900 a centred line <= 900 px; nothing animated in y 250..420 (covered by y >= 440).
const { chromium } = require('playwright');
const path = require('path');

const [page, FROM, TO] = process.argv.slice(2);
if (!page) { console.error('usage: node ads/video/tools/textcheck.js <page.html> [from] [to]'); process.exit(2) }
(async () => {
  const browser = await chromium.launch();
  const p = await (await browser.newContext({ viewport: { width: 1080, height: 1920 } })).newPage();
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  await p.goto('file://' + path.resolve(page.split('?')[0]), { waitUntil: 'networkidle' });
  await p.waitForFunction(() => window.READY === true, null, { timeout: 60000 });
  const { D, free } = await p.evaluate(() => ({ D: window.DURATION, free: (window.CUES || []).filter(c => c.type === 'free').map(c => [c.t, c.t + (c.dur || 0)]) }));
  const a = FROM ? +FROM : 0, b = TO ? +TO : D;
  const bad = [];
  let frames = 0, boxes = 0;
  for (let i = Math.ceil(a * 30 - 1e-6); i / 30 <= b + 1e-9; i++) {
    const t = i / 30;
    if (free.some(([x, y]) => t >= x - 1e-6 && t <= y + 1e-6)) continue;
    frames++;
    const lines = await p.evaluate(t => {
      window.seek(t);
      const blurOf = cs => {
        let b = 0; const f = cs.filter;
        if (!f || f === 'none') return 0;
        for (const m of f.matchAll(/blur\(([\d.]+)px\)/g)) b += +m[1];
        for (const m of f.matchAll(/url\("?#([\w-]+)"?\)/g)) {
          const fe = document.querySelector('#' + m[1] + ' feGaussianBlur');
          if (fe) b += Math.max(...(fe.getAttribute('stdDeviation') || '0').trim().split(/[\s,]+/).map(Number));
        }
        return b;
      };
      const out = [];
      const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = tw.nextNode())) {
        const el = n.parentElement;
        if (!el || /^(SCRIPT|STYLE)$/.test(el.tagName) || !n.nodeValue.trim() || el.closest('svg')) continue;
        let o = 1, bl = 0, hidden = false, block = null;
        for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
          const cs = getComputedStyle(e);
          if (cs.display === 'none' || cs.visibility === 'hidden') { hidden = true; break }
          o *= +cs.opacity; bl += blurOf(cs);
          if (!block && /^(block|flex|grid)$/.test(cs.display)) block = e;
        }
        if (hidden || o < .5 || bl >= 4) continue;
        const r = document.createRange(); r.selectNodeContents(n);
        for (const q of r.getClientRects()) if (q.width > .5 && q.height > .5)
          out.push({ x0: q.left, y0: q.top, x1: q.right, y1: q.bottom, text: n.nodeValue.trim(), key: block ? (block.id || block.className || block.tagName) : 'body' });
      }
      for (const q of (window.TEXTBOX || [])) out.push({ ...q, key: 'TEXTBOX:' + q.text });
      // merge boxes of the same block that sit on the same line
      const L = [];
      for (const q of out) {
        const cy = (q.y0 + q.y1) / 2, h = q.y1 - q.y0;
        const m = L.find(l => l.key === q.key && Math.abs((l.y0 + l.y1) / 2 - cy) < .35 * Math.max(h, l.y1 - l.y0));
        if (m) { m.x0 = Math.min(m.x0, q.x0); m.x1 = Math.max(m.x1, q.x1); m.y0 = Math.min(m.y0, q.y0); m.y1 = Math.max(m.y1, q.y1); m.text += ' ' + q.text }
        else L.push({ ...q });
      }
      return L;
    }, t);
    for (const l of lines) {
      boxes++;
      const w = l.x1 - l.x0, cx = (l.x0 + l.x1) / 2, centred = Math.abs(cx - 540) < 40, why = [];
      if (l.x0 < 90 - .5 || l.x1 > 990 + .5) why.push('x outside 90..990');
      if (l.y0 < 440 - .5 || l.y1 > 1420 + .5) why.push('y outside 440..1420');
      if (l.y1 > 900 && l.x1 > 940 + .5) why.push('below y900 x1 > 940');
      if (l.y1 > 900 && centred && w > 800 + .5) why.push('below y900 centred line wider than 800');
      if (l.y1 <= 900 && centred && w > 900 + .5) why.push('above y900 centred line wider than 900');
      if (why.length) bad.push({ t: +t.toFixed(3), text: l.text.slice(0, 40), box: [l.x0, l.y0, l.x1, l.y1].map(v => Math.round(v)), why: why.join('; ') });
    }
  }
  await browser.close();
  if (errs.length) console.log('page errors: ' + errs.join('; '));
  // print every violation, grouped into runs of consecutive frames with the same text + rule
  const runs = [];
  for (const v of bad) {
    const r = runs[runs.length - 1];
    if (r && r.text === v.text && r.why === v.why && Math.abs(v.t - r.t1 - 1 / 30) < 1e-3) { r.t1 = v.t; r.n++; if (v.box[2] - v.box[0] > r.box[2] - r.box[0]) r.box = v.box }
    else runs.push({ t0: v.t, t1: v.t, n: 1, text: v.text, why: v.why, box: v.box });
  }
  for (const r of runs) console.log(`t ${r.t0.toFixed(3)}..${r.t1.toFixed(3)} (${r.n} fr)  "${r.text}"  [${r.box}]  ${r.why}`);
  console.log(`textcheck: ${frames} frames, ${boxes} line boxes, ${bad.length} violations -> ${bad.length || errs.length ? 'FAIL' : 'PASS'}`);
  process.exit(bad.length || errs.length ? 1 : 0);
})();
