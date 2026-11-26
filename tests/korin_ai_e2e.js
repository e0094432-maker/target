// Browser test for korin/ai/index.html (Körin AI prototype).
// Serves the repo with python3 -m http.server on a free port, stubs every Körin API call with page.route
// (nothing real is sent), walks the main flows at 390x664 and 1280x800 and fails on any console error.
// Run:  node tests/korin_ai_e2e.js [screenshot-dir]
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');
const fs = require('fs');

const ROOT = path.resolve(__dirname, '..');
const OUT = process.argv[2] || '';
if (OUT) fs.mkdirSync(OUT, { recursive: true });

const freePort = () => new Promise(res => { const s = net.createServer(); s.listen(0, () => { const p = s.address().port; s.close(() => res(p)) }) });
const assert = (ok, msg) => { if (!ok) throw new Error('FAIL: ' + msg); console.log('ok -', msg) };

const QS = ['курсы английского в городе Алматы: кого посоветуешь?', 'курсы английского с хорошими отзывами, Алматы — назови конкретные компании', 'Алматы қаласында курсы английского: қай компанияны ұсынасың?'];
const ANS = [
  'В Алматы стоит присмотреться к «Lingva Test» и «Speak Test»: у них много отзывов на 2ГИС, понятные программы для детей и взрослых и пробный урок. Перед оплатой сравните размер групп.',
  'С хорошими отзывами часто называют «Аққу English», «Lingva Test» и «Speak Test». У «Аққу English» хвалят преподавателей и маленькие группы.',
  'Алматыда «Speak Test» және «Lingva Test» жиі ұсынылады: пікірлері көп, бағдарламалары түсінікті.'
];

async function stub(page, opts = {}) {
  const calls = [];
  await page.route(/fonts\.(googleapis|gstatic)\.com/, r => r.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  await page.route('https://korin-api.onrender.com/**', async route => {
    const req = route.request(), url = new URL(req.url()), body = req.postDataJSON ? (() => { try { return req.postDataJSON() } catch (e) { return null } })() : null;
    calls.push({ path: url.pathname, body });
    const json = (status, obj) => route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(obj) });
    if (opts.down && opts.down()) return route.abort('connectionrefused');
    if (url.pathname === '/api/health') return json(200, { ok: true, ai: true, provider: 'gemini', threads: false });
    if (url.pathname === '/api/quick') { const i = body.i; return json(200, { company: body.company, result: { question: QS[i], answer: ANS[i], named: i === 1, sources: i === 0 ? ['2gis.kz', 'instagram.com'] : ['2gis.kz'], model: 'gemini-2.5-flash', cached: i === 2, engine: 'gemini', search: true } }) }
    if (url.pathname === '/api/site-audit') return json(200, { url: 'https://akku-test.kz/', robots: 'read', bots: [['OAI-SearchBot', 'поиск в ChatGPT', true], ['GPTBot', 'обучение моделей OpenAI', false], ['ChatGPT-User', 'когда человек просит ChatGPT открыть страницу', true], ['PerplexityBot', 'Perplexity', true], ['Google-Extended', 'Gemini', true], ['ClaudeBot', 'Claude', true], ['bingbot', 'поиск Bing', true]].map(([bot, what, allowed]) => ({ bot, what, allowed })), checks: [
      { id: 'robots', label: 'Файл robots.txt найден', ok: true, detail: 'код ответа 200' }, { id: 'open', label: 'Главная страница открывается без входа', ok: true, detail: 'код ответа 200' }, { id: 'text', label: 'Текст виден без JavaScript', ok: false, detail: '120 знаков текста в коде страницы' }, { id: 'title', label: 'Есть заголовок и описание страницы', ok: true, detail: 'теги title и meta description' }, { id: 'price', label: 'На странице упомянуты цены', ok: false, detail: 'слова о цене или знак ₸' }, { id: 'contact', label: 'Есть телефон или адрес', ok: true, detail: 'телефон или адрес в тексте' }, { id: 'schema', label: 'Есть разметка данных о компании', ok: false, detail: 'блок JSON-LD' }, { id: 'speed', label: 'Страница отвечает быстрее трёх секунд', ok: true, detail: '0.84 с' }, { id: 'sitemap', label: 'Есть карта сайта для Bing и других поисковиков', ok: true, detail: 'sitemap.xml найден' }] });
    if (url.pathname === '/api/agent') return json(200, { text: '## Вопросы и ответы\n\n**С какого возраста вы берёте детей?**\nМы занимаемся с детьми от [что вписать] лет.\n\n**Сколько человек в группе?**\nВ группе до [что вписать] учеников.', model: 'gemini-2.5-flash', steps: [{ text: 'Сайт: прочитал 2400 знаков', ok: true }, { text: 'Взял из замера вопросы, где компанию не назвали: 2', ok: true }, { text: 'Текст написала нейросеть gemini-2.5-flash', ok: true }] });
    if (url.pathname === '/api/analyze') return json(200, { model: 'gemini-2.5-flash', text: '', steps: [{ text: 'Перечитал ответы нейросети: 3', ok: true }, { text: 'Прочитал ваш сайт: 2400 знаков', ok: true }, { text: 'Разбор написала нейросеть gemini-2.5-flash', ok: true }], analysis: { verdict: 'Конкурентов называют за отзывы на 2ГИС и подробные страницы программ.', reasons: [{ rival: 'Lingva Test', why: 'много свежих отзывов', quote: 'у них много отзывов на 2ГИС' }], gaps: [{ title: 'Соберите 10 отзывов на 2ГИС', why: 'у конкурентов их больше', how: 'отправьте просьбу пяти клиентам', effort: 'день' }] } });
    return json(404, { error: 'Не найдено.' });
  });
  return calls;
}

function watch(page, errors) {
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) });
  page.on('pageerror', e => errors.push(String(e)));
}

async function noHScroll(page, label) {
  const w = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth]);
  assert(w[0] <= w[1], `${label}: no horizontal scroll (${w[0]} <= ${w[1]})`);
}

async function measure(page) {
  await page.click('[data-start="measure"]');
  await page.waitForSelector('text=Как называется ваша компания?');
  await page.fill('#inp', 'Аққу English'); await page.press('#inp', 'Enter');
  await page.waitForSelector('text=Чем занимается компания?');
  await page.fill('#inp', 'курсы английского'); await page.press('#inp', 'Enter');
  await page.waitForSelector('text=В каком городе ваши клиенты?');
  await page.click('.chip[data-say="Алматы"]');
  await page.waitForSelector('.art-card');
}

(async () => {
  const port = await freePort();
  const srv = spawn('python3', ['-m', 'http.server', String(port), '--bind', '127.0.0.1'], { cwd: ROOT, stdio: 'ignore' });
  await new Promise(r => setTimeout(r, 700));
  const URL_ = `http://127.0.0.1:${port}/korin/ai/`;
  const browser = await chromium.launch();
  const shot = async (page, name) => { if (!OUT) return; await page.waitForTimeout(400); await page.evaluate(() => document.getElementById('toast').classList.remove('on')); await page.waitForTimeout(250); await page.screenshot({ path: path.join(OUT, name) }) };
  const errors = [];
  try {
    /* ---------- desktop ---------- */
    let ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, permissions: ['clipboard-read', 'clipboard-write'] });
    let page = await ctx.newPage(); watch(page, errors);
    let calls = await stub(page);
    await page.goto(URL_);
    await page.waitForSelector('text=Что сделать для вашего бизнеса?');
    await page.waitForSelector('text=Сервер на связи');
    assert(!(await page.content()).match(/₸\s*\d|\d\s*₸|тенге\/мес|тариф/i), 'no prices on the page');
    await shot(page, 'desktop-1-start.png');

    await measure(page);
    await page.waitForSelector('.panel .stat', { state: 'visible', timeout: 3000 }).catch(() => {});
    assert(await page.isVisible('.panel .stat'), 'measurement report opens in the side panel');
    await page.waitForTimeout(350);
    assert((await page.textContent('.panel .stat')).includes('1 из 3'), 'report counts 1 of 3 named');
    assert((await page.textContent('#conv')).includes('Gemini · gemini-2.5-flash · с поиском в интернете'), 'engine + search label from the response');
    const q = calls.filter(c => c.path === '/api/quick');
    assert(q.length === 3 && q.every((c, i) => c.body.i === i && c.body.company === 'Аққу English' && c.body.niche === 'курсы английского' && c.body.city === 'Алматы'), '/api/quick called 3 times with company, niche, city, i');
    await page.click('.panel [data-copy]');
    assert((await page.evaluate(() => navigator.clipboard.readText())).includes('Замер видимости: Аққу English'), 'copy puts the report into the clipboard');
    await shot(page, 'desktop-2-measurement.png');

    await page.click('.next .chip[data-intent="why"]');
    await page.waitForSelector('.panel .verdict');
    const an = calls.find(c => c.path === '/api/analyze');
    assert(an && an.body.answers.length === 3 && an.body.answers[1].a === ANS[1], '/api/analyze gets the real answers from the measurement');

    await page.fill('#inp', 'проверь мой сайт akku-test.kz'); await page.press('#inp', 'Enter');
    await page.waitForSelector('.panel .chk');
    assert(calls.find(c => c.path === '/api/site-audit').body.url === 'akku-test.kz', '/api/site-audit called with the site from the message');
    await shot(page, 'desktop-3-audit.png');

    await page.click('.next .chip[data-intent="faq"]');
    await page.waitForSelector('#draft');
    const ag = calls.find(c => c.path === '/api/agent');
    assert(ag.body.task === 'faq' && ag.body.context.links['Сайт'] === 'akku-test.kz' && ag.body.context.not_named_on.length === 2, '/api/agent faq gets site link and not_named_on');
    const pub = page.locator('.panel button:has-text("Одобрить и опубликовать")');
    assert(await pub.isDisabled() && (await pub.textContent()).includes('скоро'), 'publish is disabled and marked «скоро»');
    await shot(page, 'desktop-4-draft.png');

    await page.click('[data-act="conn"]');
    await page.waitForSelector('.cn-grid');
    const cards = await page.$$eval('.cn-card', els => els.map(e => e.textContent));
    for (const n of ['Telegram', 'WordPress', 'Instagram', 'Threads', 'Google Analytics', 'Битрикс24', 'amoCRM', 'Google Карты', 'WhatsApp', '2ГИС', 'Kaspi', 'Tilda']) assert(cards.some(c => c.includes(n)), `connector ${n} listed`);
    assert(cards.every(c => c.includes('скоро')) && !(await page.textContent('.cn')).match(/подключено/i), 'every connector is «скоро», none «подключено»');
    await shot(page, 'desktop-5-connectors.png');

    await page.click('[data-lang="kk"]');
    await page.click('[data-act="new"]');
    assert(await page.isVisible('text=Бизнесіңіз үшін не істейік?'), 'қаз switch changes UI strings');
    await page.click('[data-lang="ru"]');

    await page.reload();
    await page.waitForSelector('.item:has-text("Аққу English")');
    assert(true, 'company survives reload (localStorage)');
    await page.click('[data-act="sample"]');
    assert((await page.textContent('.panel')).includes('пример'), 'sample report is marked «пример»');
    await ctx.close();

    /* ---------- server down: honest error, retry ---------- */
    ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    page = await ctx.newPage();
    const downErrors = []; page.on('pageerror', e => errors.push(String(e)));
    page.on('console', m => { if (m.type() === 'error') downErrors.push(m.text()) });
    let down = true; calls = await stub(page, { down: () => down });
    await page.goto(URL_);
    await page.waitForSelector('text=Сервер не отвечает');
    await measure(page).catch(() => {});
    await page.waitForSelector('.err');
    assert((await page.textContent('.err')).includes('ничего не придумываю') && !(await page.isVisible('.art-card')), 'server down: says so, shows no result');
    await shot(page, 'desktop-6-server-down.png');
    down = false;
    await page.click('[data-retry]');
    await page.waitForSelector('.art-card');
    assert(true, 'retry works once the server is back');
    assert(downErrors.every(e => /Failed to load resource|ERR_CONNECTION_REFUSED|ERR_FAILED/.test(e)), 'only network errors from the stubbed outage');
    await ctx.close();

    /* ---------- mobile 390x664 ---------- */
    ctx = await browser.newContext({ viewport: { width: 390, height: 664 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
    page = await ctx.newPage(); watch(page, errors);
    await stub(page);
    await page.goto(URL_);
    await page.waitForSelector('text=Что сделать для вашего бизнеса?');
    assert(!(await page.locator('.side').boundingBox()) || (await page.locator('.side').boundingBox()).x < 0, 'sidebar is collapsed on mobile');
    await noHScroll(page, 'mobile start');
    await shot(page, 'mobile-1-start.png');
    await measure(page);
    await noHScroll(page, 'mobile conversation');
    await shot(page, 'mobile-2-conversation.png');
    await page.click('.art-card');
    await page.waitForTimeout(400);
    await shot(page, 'mobile-3-report.png');
    await page.click('[data-act="close-art"]');
    await page.click('.top [data-act="menu"]');
    await page.waitForTimeout(350);
    await shot(page, 'mobile-4-menu.png');
    await page.click('[data-act="conn"]');
    await page.waitForSelector('.cn-grid');
    await noHScroll(page, 'mobile connectors');
    await shot(page, 'mobile-5-connectors.png');
    await ctx.close();

    assert(errors.length === 0, 'no console errors' + (errors.length ? ': ' + errors.join(' | ') : ''));
    console.log('\nall passed');
  } catch (e) {
    console.error(e.message || e); if (errors.length) console.error('console errors:', errors);
    process.exitCode = 1;
  } finally {
    await browser.close(); srv.kill();
  }
})();
