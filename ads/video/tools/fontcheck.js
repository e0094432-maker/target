// Checks that every Onest weight the page uses is really loaded (no frame on a fallback font).
// Usage (from the repo root): node ads/video/tools/fontcheck.js ads/video/channel/<slug>.html
// Weights = font-weight of every element that holds text + window.FONT_WEIGHTS (canvas text).
// A weight passes when a loaded Onest FontFace of that weight exists, document.fonts.check() is true
// and canvas text in that weight measures differently from the same text in a fallback font.
const { chromium } = require('playwright');
const path = require('path');

const [page] = process.argv.slice(2);
if (!page) { console.error('usage: node ads/video/tools/fontcheck.js <page.html>'); process.exit(2) }
(async () => {
  const browser = await chromium.launch();
  const p = await (await browser.newContext({ viewport: { width: 1080, height: 1920 } })).newPage();
  const errs = [], failed = [];
  p.on('pageerror', e => errs.push(e.message));
  p.on('requestfailed', r => { if (/font/.test(r.url())) failed.push(r.url() + ' ' + (r.failure() || {}).errorText) });
  const url = 'file://' + path.resolve(page.split('?')[0]);
  await p.goto(url, { waitUntil: 'networkidle' });
  const ok = await p.waitForFunction(() => window.READY === true, null, { timeout: 60000 }).then(() => true, () => false);
  const res = await p.evaluate(() => {
    const ws = new Set((window.FONT_WEIGHTS || []).map(String));
    const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n, sample = '';
    while ((n = tw.nextNode())) {
      const el = n.parentElement; if (!el || /^(SCRIPT|STYLE)$/.test(el.tagName) || !n.nodeValue.trim()) continue;
      ws.add(String(getComputedStyle(el).fontWeight)); sample += n.nodeValue;
    }
    sample = (sample.replace(/\s+/g, '') || 'АБВабв') + '0123456789';
    const c = document.createElement('canvas').getContext('2d');
    return [...ws].sort().map(W => {
      const face = [...document.fonts].some(f => /Onest/.test(f.family) && String(f.weight) === W && f.status === 'loaded');
      const check = document.fonts.check(`${W} 100px Onest`, sample);
      c.font = `${W} 100px Onest, monospace`; const a = c.measureText(sample).width;
      c.font = `${W} 100px monospace`; const b = c.measureText(sample).width;
      return { weight: W, face, check, differsFromFallback: Math.abs(a - b) > 1, ok: face && check && Math.abs(a - b) > 1 };
    });
  });
  await browser.close();
  if (!ok) console.log('READY never became true' + (errs.length ? ': ' + errs.join('; ') : ''));
  if (failed.length) console.log('failed font requests:\n  ' + failed.join('\n  '));
  for (const r of res) console.log(`Onest ${r.weight}: ${r.ok ? 'OK' : 'FAIL'} (face loaded ${r.face}, check ${r.check}, differs from fallback ${r.differsFromFallback})`);
  const all = ok && !errs.length && res.every(r => r.ok);
  console.log(all ? 'fontcheck: PASS' : 'fontcheck: FAIL');
  process.exit(all ? 0 : 1);
})();
