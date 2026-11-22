// Renders an HTML animation to MP4, frame by frame, so every frame is exact.
// The page must define window.DURATION (seconds) and window.seek(t) that draws the state at time t.
// Usage: node ads/video/render.js <page.html> <out.mp4> [width] [height] [fps] [from] [to]
// MB=4 node ... renders 4 sub-frames per frame and blends them: real motion blur on fast moves (4x slower).
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');

const [page, out, W = 1920, H = 1080, FPS = 30, FROM, TO] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch();
  const p = await (await browser.newContext({ viewport: { width: +W, height: +H } })).newPage();
  p.on('pageerror', e => console.error('page error:', e.message));
  const url = /^(https?|file):/.test(page) ? page : 'file://' + path.resolve(page.split('?')[0]) + (page.includes('?') ? '?' + page.split('?')[1] : '');
  await p.goto(url, { waitUntil: 'networkidle' });
  await p.evaluate(() => document.fonts.ready);
  // the page and any frame inside it (the laptop mock-up holds the scene in an iframe) can each have seek(t)
  const ready = () => window.READY === undefined || window.READY === true;
  await p.waitForFunction(ready);
  let frames = [];
  for (let k = 0; k < 100 && !frames.length; k++) {
    for (const f of p.frames()) { await f.evaluate(() => document.fonts.ready).catch(() => {}); if (await f.evaluate(() => typeof window.seek === 'function').catch(() => false)) frames.push(f) }
    if (!frames.length) await p.waitForTimeout(100);
  }
  for (const f of frames) await f.waitForFunction(ready);
  const dur = Math.max(...await Promise.all(frames.map(f => f.evaluate(() => window.DURATION || 0))));
  const MB = Math.max(1, +(process.env.MB || 1)), RATE = FPS * MB;
  const a = FROM ? +FROM : 0, b = TO ? +TO : dur, n = Math.round((b - a) * RATE);
  const blur = MB > 1 ? ['-vf', `tmix=frames=${MB},fps=${FPS}`] : [];
  const ff = spawn('ffmpeg', ['-v', 'error', '-y', '-f', 'image2pipe', '-framerate', String(RATE), '-i', '-', ...blur,
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  for (let i = 0; i < n; i++) {
    for (const f of frames) await f.evaluate(t => window.seek(t), a + i / RATE);
    ff.stdin.write(await p.screenshot({ type: 'jpeg', quality: 94 }));
    if (i % 90 === 0) process.stdout.write(`frame ${i}/${n}\r`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
  await browser.close();
  console.log(`\ndone ${out} (${n} frames, ${(n / RATE).toFixed(1)}s${MB > 1 ? ', motion blur x' + MB : ''})`);
})();
