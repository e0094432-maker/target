// Prints the sound cue list of a page: {duration, cues:[{t,type,dur?}]}.
// Usage (from the repo root): node ads/video/tools/cues.js ads/video/channel/<slug>.html > cues.json
const { chromium } = require('playwright');
const path = require('path');

const [page] = process.argv.slice(2);
if (!page) { console.error('usage: node ads/video/tools/cues.js <page.html>'); process.exit(2) }
(async () => {
  const browser = await chromium.launch();
  const p = await (await browser.newContext({ viewport: { width: 1080, height: 1920 } })).newPage();
  const errs = [];
  p.on('pageerror', e => errs.push(e.message));
  const url = /^(https?|file):/.test(page) ? page : 'file://' + path.resolve(page.split('?')[0]) + (page.includes('?') ? '?' + page.split('?')[1] : '');
  await p.goto(url, { waitUntil: 'networkidle' });
  await p.waitForFunction(() => window.READY === true, null, { timeout: 60000 }).catch(() => {});
  if (errs.length) { console.error('page error:', errs.join('; ')); await browser.close(); process.exit(1) }
  const r = await p.evaluate(() => ({ duration: window.DURATION, cues: window.CUES }));
  await browser.close();
  if (!Array.isArray(r.cues) || !(r.duration > 0)) { console.error('page has no window.CUES / DURATION'); process.exit(1) }
  r.cues = r.cues.slice().sort((a, b) => a.t - b.t);
  process.stdout.write(JSON.stringify(r, null, 1) + '\n');
})();
