// Browser test for the Vega Legal mini-site (vega/): all five pages at 390x664 and 1280x800, with and without JavaScript.
// Serves the repo with python3 -m http.server on a free port. Google Sheet (script.google.com) and wa.me
// are stubbed with page.route (Google Fonts load for real), so nothing real is sent. Fails on any console error.
// Run:  node tests/vega_e2e.js [screenshot-dir]
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const OUT = process.argv[2] || '';
if (OUT) fs.mkdirSync(OUT, { recursive: true });
const PAGES = ['', 'mfca/', 'astana-hub/', 'en/', 'kz/'];
const SIZES = [[390, 664], [1280, 800]];

const freePort = () => new Promise(res => { const s = net.createServer(); s.listen(0, () => { const p = s.address().port; s.close(() => res(p)) }) });
const assert = (ok, msg) => { if (!ok) throw new Error('FAIL: ' + msg); console.log('ok -', msg) };
const shot = (p, name) => OUT ? p.screenshot({ path: path.join(OUT, name + '.png'), fullPage: true }) : null;
const slug = pg => (pg.replace(/\/$/, '') || 'home');

(async () => {
  const port = await freePort();
  const srv = spawn('python3', ['-m', 'http.server', String(port), '--bind', '127.0.0.1'], { cwd: ROOT, stdio: 'ignore' });
  await new Promise(r => setTimeout(r, 800));
  const base = `http://127.0.0.1:${port}/vega/`;
  const browser = await chromium.launch();
  const sheet = [];
  const stub = async ctx => {
    await ctx.route(/script\.google\.com/, r => { sheet.push(JSON.parse(r.request().postData() || '{}')); r.fulfill({ status: 200, body: 'ok' }) });
    await ctx.route(/wa\.me/, r => r.fulfill({ status: 200, contentType: 'text/html', body: '<title>wa</title>' }));
  };
  try {
    for (const [w, h] of SIZES) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      await stub(ctx);
      for (const pg of PAGES) {
        const p = await ctx.newPage(), errors = [];
        p.on('console', m => { if (m.type() === 'error') errors.push(m.text()) });
        p.on('pageerror', e => errors.push(String(e)));
        await p.goto(base + pg + '?utm_source=test-src', { waitUntil: 'load' });
        const info = await p.evaluate(() => ({
          h1: document.querySelectorAll('h1').length, title: document.title, desc: !!document.querySelector('meta[name=description]')?.content,
          canon: document.querySelector('link[rel=canonical]')?.href, og: !!document.querySelector('meta[property="og:title"]'),
          ld: [...document.querySelectorAll('script[type="application/ld+json"]')].map(s => JSON.parse(s.textContent)['@graph'].map(n => n['@type'])).flat(),
          wa: [...document.querySelectorAll('a[href^="https://wa.me/77021686733?text="]')].length,
          overflow: document.documentElement.scrollWidth - innerWidth,
          disclaimer: document.body.innerText.includes('не является юридической консультацией') || document.body.innerText.includes('not legal advice') || document.body.innerText.includes('заңгерлік кеңес болып табылмайды'),
          footer: document.querySelector('footer').innerText.includes('ТОО «SciCom», БИН 250640020511'),
          guarantee: /гарантируем|гарантия статуса|we guarantee|кепілдік береміз/i.test(document.body.innerText)
        }));
        const tag = `${slug(pg)} ${w}px`;
        assert(info.h1 === 1 && info.title && info.desc && info.og, `${tag}: one h1, title, description, Open Graph`);
        assert(info.canon === 'https://kenesai.asia/vega/' + pg, `${tag}: canonical ${info.canon}`);
        assert(info.ld.includes('LegalService'), `${tag}: LegalService JSON-LD (${info.ld.join(', ')})`);
        if (pg === 'mfca/' || pg === 'astana-hub/') assert(info.ld.includes('FAQPage'), `${tag}: FAQPage JSON-LD`);
        assert(info.wa >= 2, `${tag}: WhatsApp buttons with prefilled text (${info.wa})`);
        assert(info.overflow <= 0, `${tag}: no horizontal scroll`);
        assert(info.disclaimer && info.footer && !info.guarantee, `${tag}: disclaimer, legal entity in footer, no guarantees`);
        await shot(p, `${slug(pg)}-${w}`);
        if (w === 390 && pg === '') {
          const on = () => p.evaluate(() => document.querySelector('.wa-float').classList.contains('on'));
          assert(!(await on()), `${tag}: floating WhatsApp hidden while hero buttons are visible`);
          await p.evaluate(() => scrollTo(0, innerHeight * 2)); await p.waitForTimeout(400);
          assert(await on(), `${tag}: floating WhatsApp shown after scrolling`);
          await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(300);
          // меню-«гамбургер» работает на CSS (checkbox), скрипт закрывает его после выбора пункта
          const navShown = () => p.evaluate(() => getComputedStyle(document.querySelector('.top .nav')).display !== 'none');
          assert(!(await navShown()), `${tag}: phone menu closed at start`);
          await p.click('.burger'); assert(await navShown(), `${tag}: burger opens the menu`);
          await p.click('.top .nav a[href="#contact"]'); await p.waitForTimeout(200);
          assert(!(await navShown()), `${tag}: menu closes after choosing an item`);
          await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(300);
        }
        assert(!(await p.$('.hero .eyebrow')), `${tag}: no eyebrow label above the hero heading`);
        if (pg === '' ) {
          // форма: проверки и отправка в таблицу
          await p.click('#leadForm button');
          assert((await p.textContent('#leadForm .msg')).includes('Заполните'), `${tag}: empty form asks to fill fields`);
          await p.fill('#leadForm [name=name]', 'Тест'); await p.fill('#leadForm [name=company]', 'ТОО Тест'); await p.fill('#leadForm [name=whatsapp]', '123');
          await p.click('#leadForm button');
          assert((await p.textContent('#leadForm .msg')).includes('полностью'), `${tag}: short phone rejected`);
          const before = sheet.length;
          await p.fill('#leadForm [name=whatsapp]', '+7 701 234 56 78'); await p.click('#leadForm button');
          await p.waitForFunction(() => document.querySelector('#leadForm .msg').textContent.startsWith('Спасибо'));
          const row = sheet.slice(before).find(r => r.name === 'Vega: Тест');
          assert(row && row.business === 'ТОО Тест' && row.whatsapp === '+7 701 234 56 78', `${tag}: lead row sent ${JSON.stringify(row)}`);
          assert(/^vega-ru · test-src · посетитель \w+$/.test(row.lang), `${tag}: lang column has source and visitor id (${row.lang})`);
          await shot(p, `home-${w}-form-sent`);
          const [wa] = await Promise.all([ctx.waitForEvent('page'), p.click('.hero a[href^="https://wa.me/"]')]);
          assert(decodeURIComponent(wa.url()).includes('Здравствуйте! Нужна консультация'), `${tag}: WhatsApp opens with prefilled text`);
          await wa.close();
        }
        assert(errors.length === 0, `${tag}: no console errors ${errors.join(' | ')}`);
        await p.close();
      }
      await ctx.close();
    }

    // визиты пишутся в таблицу (в автотесте navigator.webdriver выключает это, поэтому подменяем его)
    {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 664 } });
      await stub(ctx); await ctx.addInitScript(() => Object.defineProperty(navigator, 'webdriver', { get: () => false }));
      const p = await ctx.newPage(), before = sheet.length;
      await p.goto(base + 'mfca/?src=gemini'); await p.waitForTimeout(300);
      const visit = sheet.slice(before).find(r => r.name === 'Vega: зашёл');
      assert(visit && visit.lang === 'vega' && visit.business.includes('откуда: gemini') && visit.business.includes('/mfca/') && /^посетитель \w+ · /.test(visit.whatsapp), `visit logged ${JSON.stringify(visit)}`);
      await p.click('.hero a[href^="https://wa.me/"]'); await p.waitForTimeout(300);
      assert(sheet.slice(before).some(r => r.name === 'Vega: нажал «WhatsApp»'), 'WhatsApp click logged');
      await ctx.close();
    }

    // без JavaScript весь текст на месте
    for (const [w, h] of SIZES) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, javaScriptEnabled: false });
      await stub(ctx);
      for (const pg of PAGES) {
        const p = await ctx.newPage();
        await p.goto(base + pg);
        const r = await p.evaluate(() => ({ h1: document.querySelector('h1').getBoundingClientRect().height > 0, text: document.body.innerText.length, faq: document.querySelectorAll('details').length, noscript: document.body.innerText.includes('JavaScript') }));
        const tag = `${slug(pg)} ${w}px no-JS`;
        assert(r.h1 && r.text > 1500 && r.noscript, `${tag}: heading and ${r.text} chars of text visible, form explains WhatsApp fallback`);
        if (pg === 'mfca/' || pg === 'astana-hub/') {
          await p.evaluate(() => document.querySelectorAll('details').forEach(d => d.open = true));
          assert(r.faq >= 6, `${tag}: ${r.faq} FAQ answers in the HTML`);
        }
        if (w === 390 || pg === 'mfca/') await shot(p, `${slug(pg)}-${w}-nojs`);
        await p.close();
      }
      await ctx.close();
    }
    console.log('\nall vega checks passed');
  } finally { await browser.close(); srv.kill() }
})().catch(e => { console.error(e.message || e); process.exit(1) });
