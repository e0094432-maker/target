// The marketer's designer: turns today's post into two or three pictures in the Körin style (1080×1350).
// Usage: node social/cards.js <plan.json> <out dir>. Prints a JSON array of file names.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [planFile, out = 'korin/media'] = process.argv.slice(2);
const plan = JSON.parse(fs.readFileSync(planFile, 'utf8'));
const esc = t => String(t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const THEMES = [
  { bg: '#0a0a0b', ink: '#edf2f5', mute: 'rgba(237,242,245,.62)', ac: '#8b9cff', glow: 'rgba(139,156,255,.34)', line: 'rgba(255,255,255,.09)', chip: 'rgba(139,156,255,.16)' },
  { bg: '#f4f5f7', ink: '#0c0d10', mute: 'rgba(12,13,16,.6)', ac: '#4f5bd5', glow: 'rgba(79,91,213,.22)', line: 'rgba(12,13,16,.08)', chip: 'rgba(79,91,213,.12)' },
  { bg: '#10133a', ink: '#ffffff', mute: 'rgba(255,255,255,.66)', ac: '#aab6ff', glow: 'rgba(170,182,255,.3)', line: 'rgba(255,255,255,.1)', chip: 'rgba(170,182,255,.18)' },
];
const t = THEMES[(plan.theme ?? Math.floor(Date.now() / 864e5)) % THEMES.length];
const slides = plan.slides.slice(0, 2).map((s, i) => ({ ...s, n: i + 1 }));
slides.push({ cta: true, n: slides.length + 1 });
const total = slides.length;

const page = s => `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0}
body{width:1080px;height:1350px;background:${t.bg};color:${t.ink};font-family:"Onest",system-ui,sans-serif;position:relative;overflow:hidden;padding:84px 80px}
.glow{position:absolute;right:-260px;top:-320px;width:1100px;height:1100px;border-radius:50%;background:radial-gradient(closest-side,${t.glow},transparent)}
.ring{position:absolute;border-radius:50%;border:2px solid ${t.line}}
.r1{right:-40px;top:-100px;width:520px;height:520px}.r2{right:-230px;top:-290px;width:900px;height:900px}.r3{right:-420px;top:-480px;width:1280px;height:1280px}
.top{position:relative;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:16px;font-size:40px;font-weight:600;letter-spacing:-.02em}
.logo i{width:46px;height:46px;border-radius:50%;background:${t.ink};display:grid;place-items:center}
.logo i::before{content:"";width:16px;height:16px;border-radius:50%;background:${t.bg}}
.num{font-size:30px;color:${t.mute};font-variant-numeric:tabular-nums}
.mid{position:absolute;left:80px;right:80px;top:300px;bottom:210px;display:flex;flex-direction:column;justify-content:center;gap:44px}
.tag{align-self:flex-start;font-size:30px;font-weight:500;color:${t.ac};background:${t.chip};border-radius:999px;padding:12px 26px;letter-spacing:.04em;text-transform:uppercase}
h1{font-weight:600;letter-spacing:-.035em;line-height:1.04}
ol{list-style:none;padding:0;display:grid;gap:30px}
li{display:grid;grid-template-columns:76px 1fr;gap:26px;align-items:start;font-size:46px;line-height:1.22;font-weight:500;letter-spacing:-.015em}
li b{width:76px;height:76px;border-radius:50%;background:${t.chip};color:${t.ac};display:grid;place-items:center;font-size:36px;font-weight:600}
.cta{font-size:50px;line-height:1.25;color:${t.mute};font-weight:500;letter-spacing:-.015em}
.site{display:inline-block;margin-top:22px;font-size:56px;font-weight:600;color:${t.bg};background:${t.ink};border-radius:999px;padding:22px 44px;letter-spacing:-.02em}
.foot{position:absolute;left:80px;right:80px;bottom:80px;display:flex;justify-content:space-between;align-items:center;font-size:32px;color:${t.mute}}
.foot i{display:block;width:120px;height:6px;background:${t.ac};border-radius:3px}
</style></head><body><div class="glow"></div><div class="ring r1"></div><div class="ring r2"></div><div class="ring r3"></div>
<div class="top"><div class="logo"><i></i>Körin</div><div class="num">${s.n} / ${total}</div></div>
<div class="mid">${s.cta
  ? `<span class="tag">бесплатно</span><h1 id="h">Проверьте, советует ли вас нейросеть</h1><div class="cta">Одна минута. Без регистрации.<br><span class="site">kenesai.asia/korin</span></div>`
  : `${s.tag ? `<span class="tag">${esc(s.tag)}</span>` : ''}<h1 id="h">${esc(s.title)}</h1>${(s.points || []).length ? `<ol>${s.points.slice(0, 3).map((p, i) => `<li><b>${i + 1}</b><span>${esc(p)}</span></li>`).join('')}</ol>` : ''}`}</div>
<div class="foot"><i></i><span>${s.n < total ? 'листайте →' : '@korin.kz'}</span></div>
<script>
// the headline takes the largest size that still fits its space
(() => { const h = document.getElementById('h'), mid = document.querySelector('.mid'); let size = ${s.cta ? 104 : (s.points || []).length ? 84 : 128};
const fits = () => mid.scrollHeight <= mid.clientHeight + 2 && h.scrollWidth <= h.clientWidth + 2;
h.style.fontSize = size + 'px'; while (!fits() && size > 44) { size -= 4; h.style.fontSize = size + 'px' } })();
</script></body></html>`;

(async () => {
  fs.mkdirSync(out, { recursive: true });
  const stamp = new Date().toISOString().slice(0, 10) + '-' + Date.now().toString(36).slice(-4);
  const browser = await chromium.launch(), ctx = await browser.newContext({ viewport: { width: 1080, height: 1350 } }), p = await ctx.newPage(), files = [];
  for (const s of slides) {
    await p.setContent(page(s), { waitUntil: 'networkidle' }).catch(() => {});
    await p.evaluate(() => document.fonts.ready).catch(() => {});
    await p.waitForTimeout(300);
    const file = `${stamp}-${s.n}.png`; await p.screenshot({ path: path.join(out, file) }); files.push(file);
  }
  await browser.close();
  console.log(JSON.stringify(files));
})();
