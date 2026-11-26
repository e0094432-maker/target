// Browser test for korin/ai/index.html — the one Körin cabinet (chat, dark by default).
// Serves the repo with python3 -m http.server on a free port, stubs every Körin API call with page.route
// (nothing real is sent: Telegram and WordPress are never contacted, any request to them fails the test),
// walks the main flows at 390x664 and 1280x800, the old korin/app redirect, the Telegram and WordPress
// connectors with approval before publishing, and fails on any console error.
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

const TOKEN = '123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawA', WP_PASS = 'abcd EFGH ijkl MNOP qrst uvwx';
const QS = ['курсы английского в городе Алматы: кого посоветуешь?', 'курсы английского с хорошими отзывами, Алматы — назови конкретные компании', 'Алматы қаласында курсы английского: қай компанияны ұсынасың?'];
const ANS = [
  'В Алматы стоит присмотреться к «Lingva Test» и «Speak Test»: у них много отзывов на 2ГИС, понятные программы для детей и взрослых и пробный урок. Перед оплатой сравните размер групп.',
  'С хорошими отзывами часто называют «Аққу English», «Lingva Test» и «Speak Test». У «Аққу English» хвалят преподавателей и маленькие группы.',
  'Алматыда «Speak Test» және «Lingva Test» жиі ұсынылады: пікірлері көп, бағдарламалары түсінікті.'
];

async function stub(page, opts = {}) {
  const calls = [];
  await page.route(/fonts\.(googleapis|gstatic)\.com/, r => r.fulfill({ status: 200, contentType: 'text/css', body: '' }));
  // the browser must never talk to Telegram, WordPress or the lead sheet itself: only through the Körin server
  await page.route(/api\.telegram\.org|akku-test\.kz|script\.google\.com/, r => { calls.push({ path: 'DIRECT ' + r.request().url(), body: null }); return r.abort() });
  await page.route('https://korin-api.onrender.com/**', async route => {
    const req = route.request(), url = new URL(req.url()), body = req.postDataJSON ? (() => { try { return req.postDataJSON() } catch (e) { return null } })() : null;
    calls.push({ path: url.pathname, body });
    const json = (status, obj) => route.fulfill({ status, contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': '*' }, body: JSON.stringify(obj) });
    if (opts.down && opts.down()) return route.abort('connectionrefused');
    if (url.pathname === '/api/health') return json(200, { ok: true, ai: true, provider: 'gemini', threads: false });
    if (url.pathname === '/api/quick') { const i = body.i, rival = body.company === 'Lingva Test'; return json(200, { company: body.company, result: { question: QS[i], answer: ANS[i], named: rival ? i < 3 : i === 1, sources: i === 0 ? ['2gis.kz', 'instagram.com'] : ['2gis.kz'], model: 'gemini-2.5-flash', cached: rival || i === 2, engine: 'gemini', search: true } }) }
    if (url.pathname === '/api/site-audit') return json(200, { url: 'https://akku-test.kz/', robots: 'read', bots: [['OAI-SearchBot', 'поиск в ChatGPT', true], ['GPTBot', 'обучение моделей OpenAI', false], ['ChatGPT-User', 'когда человек просит ChatGPT открыть страницу', true], ['PerplexityBot', 'Perplexity', true], ['Google-Extended', 'Gemini', true], ['ClaudeBot', 'Claude', true], ['bingbot', 'поиск Bing', true]].map(([bot, what, allowed]) => ({ bot, what, allowed })), checks: [
      { id: 'robots', label: 'Файл robots.txt найден', ok: true, detail: 'код ответа 200' }, { id: 'open', label: 'Главная страница открывается без входа', ok: true, detail: 'код ответа 200' }, { id: 'text', label: 'Текст виден без JavaScript', ok: false, detail: '120 знаков текста в коде страницы' }, { id: 'title', label: 'Есть заголовок и описание страницы', ok: true, detail: 'теги title и meta description' }, { id: 'price', label: 'На странице упомянуты цены', ok: false, detail: 'слова о цене или знак ₸' }, { id: 'contact', label: 'Есть телефон или адрес', ok: true, detail: 'телефон или адрес в тексте' }, { id: 'schema', label: 'Есть разметка данных о компании', ok: false, detail: 'блок JSON-LD' }, { id: 'speed', label: 'Страница отвечает быстрее трёх секунд', ok: true, detail: '0.84 с' }, { id: 'sitemap', label: 'Есть карта сайта для Bing и других поисковиков', ok: true, detail: 'sitemap.xml найден' }] });
    if (url.pathname === '/api/connect/telegram/test') {
      if (body.token !== TOKEN) return json(400, { error: 'Telegram не принял токен бота. Скопируйте его в @BotFather заново (команда /mybots → API Token).' });
      return json(200, { ok: true, bot: '@akku_bot', title: 'Аққу English', chat: '@akku_channel', type: 'channel', can_post: true });
    }
    if (url.pathname === '/api/publish/telegram') return json(200, { ok: true, message_id: 77, link: 'https://t.me/akku_channel/77', title: 'Аққу English' });
    if (url.pathname === '/api/connect/wordpress/test') return json(200, { ok: true, site: 'https://akku-test.kz', name: 'Айгерим', can_publish: true });
    if (url.pathname === '/api/publish/wordpress') return json(200, { ok: true, id: 15, status: body.status, link: 'https://akku-test.kz/?page_id=15', edit_link: 'https://akku-test.kz/wp-admin/post.php?post=15&action=edit' });
    if (url.pathname === '/api/agent' && body.task === 'tgpost') return json(200, { text: 'Набор в летние группы открыт!\n\nЗанимаемся по 90 минут, в группе до 8 человек. Первый урок — пробный.\n\nЗапись — в личных сообщениях канала.', model: 'gemini-2.5-flash', steps: [{ text: 'Сайт: прочитал 2400 знаков', ok: true }, { text: 'Текст написала нейросеть gemini-2.5-flash', ok: true }] });
    // the bug the owner saw: a reply that came twice in one answer must be shown once
    if (url.pathname === '/api/agent' && body.task === 'chat') return json(200, { text: DUP + '\n\n' + DUP, model: 'gemini-2.5-flash', steps: [{ text: 'Текст написала нейросеть gemini-2.5-flash', ok: true }] });
    if (url.pathname === '/api/agent' && ['weekplan', 'reply', 'review', 'pitch', 'narrow', 'bio'].includes(body.task)) return json(200, { text: `Черновик (${body.task}).\n\nПервый пункт для «${body.context.company}».`, model: 'gemini-2.5-flash', steps: [{ text: 'Текст написала нейросеть gemini-2.5-flash', ok: true }] });
    if (url.pathname === '/api/agent') return json(200, { text: '## Вопросы и ответы\n\n**С какого возраста вы берёте детей?**\nМы занимаемся с детьми от [что вписать] лет.\n\n**Сколько человек в группе?**\nВ группе до [что вписать] учеников.', model: 'gemini-2.5-flash', steps: [{ text: 'Сайт: прочитал 2400 знаков', ok: true }, { text: 'Взял из замера вопросы, где компанию не назвали: 2', ok: true }, { text: 'Текст написала нейросеть gemini-2.5-flash', ok: true }] });
    if (url.pathname === '/api/analyze') return json(200, { model: 'gemini-2.5-flash', text: '', steps: [{ text: 'Перечитал ответы нейросети: 3', ok: true }, { text: 'Прочитал ваш сайт: 2400 знаков', ok: true }, { text: 'Разбор написала нейросеть gemini-2.5-flash', ok: true }], analysis: { verdict: 'Конкурентов называют за отзывы на 2ГИС и подробные страницы программ.', reasons: [{ rival: 'Lingva Test', why: 'много свежих отзывов', quote: 'у них много отзывов на 2ГИС' }], gaps: [{ title: 'Соберите 10 отзывов на 2ГИС', why: 'у конкурентов их больше', how: 'отправьте просьбу пяти клиентам', effort: 'день' }] } });
    return json(404, { error: 'Не найдено.' });
  });
  return calls;
}

const DUP = 'Поздравление готово: «Аққу English поздравляет вас с Наурызом!» Добавьте фото группы и время занятий в праздники.';
// saved data for the company home: three real-shaped measurements and a site check, as the page stores them
const DAY = 864e5, NOW = Date.now();
const saved = (extra = {}) => ({ lang: 'ru', co: 'c1', chat: null, chats: [], companies: [{ id: 'c1', name: 'Аққу English', niche: 'rehcs fyukbqcrjuj', city: 'Алматы', site: 'akku-test.kz',
  measures: [[NOW - 14 * DAY, 0], [NOW - 7 * DAY, 0], [NOW - 2 * DAY, 1]].map(([at, k]) => ({ at, results: QS.map((q, i) => ({ question: q, answer: ANS[i], named: i < k, sources: i === 0 ? ['2gis.kz', 'instagram.com'] : ['2gis.kz'], engine: 'gemini', model: 'gemini-2.5-flash', search: true, cached: false })) })),
  audit: { at: NOW - 2 * DAY, url: 'https://akku-test.kz/', ok: 6, total: 9, bad: ['Текст виден без JavaScript', 'На странице упомянуты цены', 'Есть разметка данных о компании'], closed: ['GPTBot'] }, ...extra }] });

let expect400 = 0;  // a stubbed «bad token» answer is logged by Chrome as a failed resource: allowed only where expected
function watch(page, errors) {
  page.on('console', m => { if (m.type() !== 'error') return; if (expect400 && /status of 400/.test(m.text())) { expect400--; return } errors.push(m.text()) });
  page.on('pageerror', e => errors.push(String(e)));
}

async function noHScroll(page, label) {
  const w = await page.evaluate(() => [document.documentElement.scrollWidth, innerWidth, [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 0.5 && getComputedStyle(e).visibility !== 'hidden' && !e.closest('.panel:not(:has(*))')).slice(0, 4).map(e => e.tagName + '.' + e.className + ' ' + Math.round(e.getBoundingClientRect().right)).join(', ')]);
  assert(w[0] <= w[1], `${label}: no horizontal scroll (${w[0]} <= ${w[1]})${w[0] > w[1] ? ' — wider: ' + w[2] : ''}`);
}

async function measure(page, niche = 'курсы английского') {
  await page.click('[data-start="measure"]');
  await page.waitForSelector('text=Как называется ваша компания?');
  await page.fill('#inp', 'Аққу English'); await page.press('#inp', 'Enter');
  await page.waitForSelector('text=Чем занимается компания?');
  await page.fill('#inp', niche); await page.press('#inp', 'Enter');
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
    const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert(await bg() === 'rgb(0, 0, 0)' && await page.getAttribute('html', 'data-theme') === 'dark', 'dark (black) design by default, like the landing');
    const ids = await page.$$eval('.start .tool', els => els.map(e => e.dataset.start));
    assert(ids.join() === 'measure,why,audit,faq,tgpost,weekplan,reply,review,bio,pitch,narrow,compare', 'start screen: gallery of 12 tools, each a real flow');
    await shot(page, 'desktop-1-start.png');
    await page.click('[data-act="theme"]');
    assert(await bg() === 'rgb(255, 255, 255)' && await page.evaluate(() => localStorage.getItem('korin-theme-v4')) === 'light', 'theme toggle switches to light and remembers it');
    await shot(page, 'desktop-1b-start-light.png');
    await page.click('[data-act="theme"]');
    assert(await bg() === 'rgb(0, 0, 0)', 'and back to dark');

    await measure(page, 'rehcs fyukbqcrjuj');  // «курсы английского» typed with the English layout on
    assert((await page.textContent('.lfix')).includes('Исправил раскладку') && (await page.textContent('.lfix')).includes('курсы английского') && await page.isVisible('.lfix [data-unfix]'), 'wrong keyboard layout is fixed and said so, with «Вернуть как было»');
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

    // free chat: the reply text appears once even when the server's answer carries it twice
    await page.fill('#inp', 'напиши поздравление клиентам с Наурызом'); await page.press('#inp', 'Enter');
    await page.waitForFunction(() => document.querySelectorAll('#conv .prose').length && [...document.querySelectorAll('#conv .prose')].pop().textContent.includes('Поздравление готово'));
    const last = await page.evaluate(() => [...document.querySelectorAll('#conv .msg-a')].pop().textContent);
    assert(last.split('Поздравление готово').length === 2, 'a chat reply is rendered once, not twice');
    assert(await page.evaluate(() => [...document.querySelectorAll('#conv .msg-a')].pop().querySelectorAll('.next .chip[data-intent]').length) >= 1, 'a chat reply ends with tool buttons, not just prose');
    const chatCall = calls.find(c => c.path === '/api/agent' && c.body.task === 'chat');
    assert(chatCall && chatCall.body.context.niche === 'курсы английского', 'the fixed niche is what goes to the server');
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
    const ag = calls.find(c => c.path === '/api/agent' && c.body.task === 'faq');
    assert(ag.body.task === 'faq' && ag.body.context.links['Сайт'] === 'akku-test.kz' && ag.body.context.not_named_on.length === 2, '/api/agent faq gets site link and not_named_on');
    assert(await page.isVisible('.panel [data-act="cn-wp"]') && !(await page.isVisible('.panel [data-act="pub"]')), 'draft offers «Подключить WordPress» before anything can be published');
    await shot(page, 'desktop-4-draft.png');

    /* WordPress: connect from the draft, then preview, then approve */
    await page.click('.panel [data-act="cn-wp"]');
    await page.waitForSelector('#cnForm');
    assert((await page.textContent('#cnForm')).includes('Пароли приложений') && (await page.textContent('#cnForm')).includes('только в вашем браузере'), 'WordPress form: steps + «ключ хранится только в вашем браузере»');
    await page.fill('#cnForm [name=site]', 'https://akku-test.kz'); await page.fill('#cnForm [name=user]', 'admin'); await page.fill('#cnForm [name=app_password]', WP_PASS);
    await page.click('#cnGo');
    await page.waitForSelector('#pubForm');  // connecting from a draft goes straight on to the preview
    const wpTest = calls.find(c => c.path === '/api/connect/wordpress/test');
    assert(wpTest && wpTest.body.app_password === WP_PASS && wpTest.body.site === 'https://akku-test.kz', '/api/connect/wordpress/test gets site, user, application password');
    assert(JSON.parse(await page.evaluate(() => localStorage.getItem('korin-conn-v1'))).wp.app_password === WP_PASS, 'WordPress key is kept in this browser only (localStorage)');
    assert(await page.isDisabled('#pubGo') && (await page.textContent('#pubForm .warnbox')).includes('[что вписать]'), 'placeholders [что вписать] block publishing');
    assert(!calls.some(c => c.path === '/api/publish/wordpress'), 'nothing is published without approval');
    await page.click('#pubForm [data-act="dlg-close"]');
    await page.fill('#draft', 'Вопросы и ответы\n\nС какого возраста вы берёте детей?\nС 6 лет.\n\nСколько человек в группе?\nДо 8 учеников.');
    await page.click('.panel [data-act="pub"]');
    await page.waitForSelector('#pubForm');
    assert(!(await page.isDisabled('#pubGo')) && (await page.textContent('#pv')).includes('До 8 учеников') && await page.isChecked('#pubForm [name=status][value=draft]'), 'preview shows the edited text, draft status is the default');
    await shot(page, 'desktop-6-publish-preview-wp.png');
    await page.click('#pubGo');
    await page.waitForSelector('.d-ok');
    const wpPub = calls.filter(c => c.path === '/api/publish/wordpress');
    assert(wpPub.length === 1 && wpPub[0].body.status === 'draft' && wpPub[0].body.title === 'Вопросы и ответы' && wpPub[0].body.content.includes('С 6 лет.'), '/api/publish/wordpress once, as a draft, with the approved text');
    assert((await page.textContent('.d-ok')).includes('Черновик страницы создан') && await page.getAttribute('#dlg a.btn', 'href') === 'https://akku-test.kz/wp-admin/post.php?post=15&action=edit', 'result links to the page in WordPress');
    await page.click('#dlg [data-act="dlg-close"]');
    assert((await page.textContent('#conv')).includes('Создал в WordPress черновик страницы'), 'the chat keeps a record of what was published');

    /* Telegram: connectors screen, bad token, good token, post with approval, disconnect */
    await page.click('[data-act="conn"]');
    await page.waitForSelector('.cn-grid');
    const cards = await page.$$eval('.cn-card', els => els.map(e => [e.dataset.cn, e.textContent]));
    for (const n of ['Telegram', 'WordPress', 'Instagram', 'Threads', 'Google Analytics', 'Битрикс24', 'amoCRM', 'Google Карты', 'WhatsApp', '2ГИС', 'Kaspi', 'Tilda']) assert(cards.some(c => c[1].includes(n)), `connector ${n} listed`);
    assert(cards.filter(c => !['tg', 'wp'].includes(c[0])).every(c => c[1].includes('скоро')), 'all but Telegram and WordPress are honestly «скоро»');
    assert(cards.find(c => c[0] === 'wp')[1].includes('подключено') && await page.isVisible('[data-act="cn-tg"]'), 'WordPress shows «подключено», Telegram offers «Подключить»');
    await shot(page, 'desktop-5-connectors.png');
    await page.click('[data-act="cn-tg"]');
    await page.waitForSelector('#cnForm');
    assert((await page.textContent('#cnForm')).includes('@BotFather') && (await page.textContent('#cnForm')).includes('Публикация сообщений') && (await page.textContent('#cnForm')).includes('только в вашем браузере'), 'Telegram form: BotFather steps, admin right, browser-only note');
    await page.fill('#cnForm [name=token]', '123456789:wrongwrongwrongwrongwrongwrongwrong'); await page.fill('#cnForm [name=chat]', '@akku_channel');
    expect400 = 1;
    await page.click('#cnGo');
    await page.waitForSelector('#cnErr:not(:empty)');
    assert((await page.textContent('#cnErr')).includes('не принял токен') && !(await page.evaluate(() => localStorage.getItem('korin-conn-v1'))).includes('wrongwrong'), 'bad token: error shown, nothing saved');
    await page.fill('#cnForm [name=token]', TOKEN);
    await shot(page, 'desktop-7-telegram-connect.png');
    await page.click('#cnGo');
    await page.waitForSelector('.cn-card[data-cn="tg"] .tag.ok');
    assert((await page.textContent('.cn-card[data-cn="tg"]')).includes('Аққу English'), 'Telegram connected: channel title from the server');

    await page.click('.item[data-chat]');
    await page.click('.next .chip[data-intent="tgpost"]');
    await page.waitForSelector('.panel [data-act="pub"]');
    assert(calls.find(c => c.path === '/api/agent' && c.body.task === 'tgpost'), '/api/agent writes the Telegram post (task tgpost)');
    await page.click('.panel [data-act="pub"]');
    await page.waitForSelector('#pubForm .pv.tgp');
    assert((await page.textContent('#pubForm .where')).includes('Аққу English') && !calls.some(c => c.path === '/api/publish/telegram'), 'Telegram preview names the channel; nothing sent yet');
    await shot(page, 'desktop-8-publish-preview-tg.png');
    await page.click('#pubGo');
    await page.waitForSelector('.d-ok');
    const tgPub = calls.filter(c => c.path === '/api/publish/telegram');
    assert(tgPub.length === 1 && tgPub[0].body.token === TOKEN && tgPub[0].body.chat === '@akku_channel' && tgPub[0].body.text.startsWith('Набор в летние группы'), '/api/publish/telegram once with token, channel and the approved text');
    assert(await page.getAttribute('#dlg a.btn', 'href') === 'https://t.me/akku_channel/77', 'result links to the post');
    await page.click('#dlg [data-act="dlg-close"]');
    await shot(page, 'desktop-9-published.png');

    await page.click('[data-act="conn"]');
    await page.click('[data-act="off-tg"]');
    const left = JSON.parse(await page.evaluate(() => localStorage.getItem('korin-conn-v1')));
    assert(!left.tg && left.wp, '«Отключить» deletes the Telegram key from the browser');
    await page.click('[data-act="off-wp"]');
    assert(await page.evaluate(() => localStorage.getItem('korin-conn-v1')) === null, 'and the WordPress one: nothing left');
    assert(!calls.some(c => c.path.startsWith('DIRECT')), 'the browser never called Telegram or WordPress directly');

    await page.click('[data-lang="kk"]');
    await page.click('[data-act="new"]');
    assert(await page.isVisible('text=Аптаның істері') && await page.isVisible('.tool:has-text("Көріну өлшемі")'), 'қаз switch changes UI strings');
    await page.click('[data-lang="ru"]');

    await page.reload();
    await page.waitForSelector('.item:has-text("Аққу English")');
    assert(true, 'company survives reload (localStorage)');
    await page.click('[data-act="sample"]');
    assert((await page.textContent('.panel')).includes('пример'), 'sample report is marked «пример»');
    await ctx.close();

    /* ---------- company home from saved data, plan reply, tools ---------- */
    for (const [vw, tag] of [[{ width: 1280, height: 800 }, 'desktop'], [{ width: 390, height: 664 }, 'mobile']]) {
      ctx = await browser.newContext(tag === 'mobile' ? { viewport: vw, isMobile: true, hasTouch: true, deviceScaleFactor: 2 } : { viewport: vw });
      page = await ctx.newPage(); watch(page, errors);
      calls = await stub(page);
      await page.addInitScript(d => { if (!sessionStorage.getItem('seeded')) { localStorage.setItem('korin-ai-v1', d); sessionStorage.setItem('seeded', '1') } }, JSON.stringify(saved()));
      await page.goto(URL_);
      await page.waitForSelector('.home .dash');
      const dash = await page.textContent('.dash');
      assert(dash.includes('1 из 3') && dash.includes('Gemini · gemini-2.5-flash · с поиском') && dash.includes('+1 к прошлому замеру'), `${tag} home: last score, date, engine and change from saved measurements`);
      assert(await page.$$eval('.spark i', e => e.length) === 3 && (await page.textContent('.spark')).includes('0/3'), `${tag} home: trend of the 3 saved measurements`);
      assert(dash.includes('Lingva Test') && dash.includes('Speak Test') && !/Аққу English\s*\d/.test(dash.split('Кого называют')[1].split('Вы ·')[0]), `${tag} home: rivals named most often, counted from saved answers (not the company itself)`);
      assert(dash.includes('2gis.kz · 9') && dash.includes('instagram.com · 3'), `${tag} home: sources cited, counted`);
      assert(dash.includes('6 из 9') && dash.includes('GPTBot закрыт в robots.txt'), `${tag} home: last site check`);
      assert((await page.textContent('.fixbar')).includes('«rehcs fyukbqcrjuj» → «курсы английского»') && (await page.textContent('.home-head')).includes('курсы английского'), `${tag} home: niche saved in the wrong layout is fixed, said so`);
      const tasks = await page.$$eval('.tasks .task', els => els.map(e => [e.querySelector('[data-tool]').dataset.tool, e.textContent]));
      assert(tasks.length === 3 && tasks[0][0] === 'faq' && tasks[0][1].includes('вас не назвали') && tasks.some(x => x[0] === 'audit' && x[1].includes('поправить 4')), `${tag} home: «Дела на неделю» — 3 tasks from real gaps (${tasks.map(x => x[0])})`);
      await shot(page, `${tag}-11-company-home.png`);
      if (tag === 'desktop') { await page.evaluate(() => document.querySelector('.tasks').scrollIntoView()); await shot(page, 'desktop-11b-company-home-tasks.png') }
      // «Вернуть как было» for the saved niche
      await page.click('[data-act="unfix-co"]');
      assert((await page.textContent('.home-head')).includes('rehcs fyukbqcrjuj') && !(await page.isVisible('.fixbar')), `${tag} undo puts the niche back as typed`);
      await page.reload(); await page.waitForSelector('.home .dash');
      assert(!(await page.isVisible('.fixbar')), `${tag} and it is not «fixed» again after reload`);
      await page.click('[data-act="edit-co"]'); await page.fill('#coForm [name=niche]', 'курсы английского'); await page.click('#coForm .btn-p');
      await page.waitForSelector('.home .dash');
      assert((await page.textContent('.dash')).includes('Сделать первый замер') && !(await page.textContent('.dash')).includes('из 3'), `${tag} changed niche: old numbers gone, clear CTA «Сделать первый замер»`);
      if (tag === 'desktop') await shot(page, 'desktop-12-company-home-empty.png');
      await page.reload();
      await page.evaluate(d => localStorage.setItem('korin-ai-v1', d), JSON.stringify(saved({ niche: 'курсы английского' })));
      await page.reload(); await page.waitForSelector('.home .dash .spark');

      // generic question → a plan made of tool buttons, no prose from the model
      const agentBefore = calls.filter(c => c.path === '/api/agent').length;
      await page.fill('#inp', 'как ты мне поможешь?'); await page.press('#inp', 'Enter');
      await page.waitForSelector('#conv .plan .task');
      const plan = await page.$$eval('#conv .plan .task', els => els.map(e => [e.querySelector('[data-tool]').dataset.tool, e.querySelector('[data-tool]').textContent, e.textContent]));
      assert(plan.length >= 4 && plan.every(x => /Сделать|Повторить/.test(x[1])) && plan[0][0] === 'faq', `${tag} «как ты мне поможешь?» → plan of tool buttons (${plan.map(x => x[0])})`);
      assert(calls.filter(c => c.path === '/api/agent').length === agentBefore, `${tag} the plan is built from saved data, no model talk`);
      await noHScroll(page, `${tag} plan`);
      await shot(page, `${tag}-13-plan-reply.png`);
      await page.click('#conv .plan .task >> nth=0 >> [data-tool]');
      await page.waitForFunction(() => document.querySelectorAll('#conv .art-card').length >= 1);
      const faq = calls.filter(c => c.path === '/api/agent' && c.body.task === 'faq').pop();
      assert(faq && faq.body.question.includes(QS[1]) && faq.body.context.site_problems.length === 4 && faq.body.context.rivals.includes('Lingva Test'), `${tag} «Сделать» runs the FAQ tool for the question where the company was not named`);
      if (tag === 'mobile') { await page.click('[data-act="close-art"]').catch(() => {}); await page.waitForTimeout(300) }

      // tools: content plan, review reply, compare with a competitor
      if (tag === 'desktop') {
        await page.click('[data-act="tools"]');
        assert(await page.$$eval('.cn .tool', e => e.length) === 12, 'sidebar «Инструменты» opens the gallery');
        await shot(page, 'desktop-14-tools.png');
        await page.click('.cn .tool[data-start="weekplan"]');
        await page.waitForSelector('.panel #draft');
        const wp = calls.filter(c => c.path === '/api/agent' && c.body.task === 'weekplan').pop();
        assert(wp && /Даты постов: (пн|вт|ср|чт|пт|сб), \d+ [а-я]+; /.test(wp.body.question) && wp.body.question.split(';').length === 5, 'content plan: /api/agent weekplan with 5 real dates');
        await page.click('[data-act="tools"]'); await page.click('.cn .tool[data-start="reply"]');
        await page.waitForSelector('text=Вставьте текст отзыва');
        await page.fill('#inp', 'Ждали администратора 20 минут, но преподаватель Айгуль супер!'); await page.press('#inp', 'Enter');
        await page.waitForSelector('.panel #draft');
        const rp = calls.filter(c => c.path === '/api/agent' && c.body.task === 'reply').pop();
        assert(rp && rp.body.question.includes('Отзыв клиента:\nЖдали администратора 20 минут'), 'review reply: the pasted review goes to /api/agent reply as is');
        await page.click('[data-act="tools"]'); await page.click('.cn .tool[data-start="review"]');
        await page.waitForSelector('.panel [data-act="wa"]');
        assert(calls.some(c => c.path === '/api/agent' && c.body.task === 'review'), 'review request: draft with «Открыть в WhatsApp»');
        for (const [id, task] of [['pitch', 'pitch'], ['narrow', 'narrow'], ['bio', 'bio']]) {
          await page.click('[data-act="tools"]'); await page.click(`.cn .tool[data-start="${id}"]`);
          await page.waitForFunction(n => [...document.querySelectorAll('#conv .msg-a')].pop().textContent.includes('Черновик готов'), id);
          assert(calls.some(c => c.path === '/api/agent' && c.body.task === task), `tool ${id} → /api/agent ${task}`);
        }
      }
      await page.click(tag === 'mobile' ? '.top [data-act="menu"]' : '[data-act="home"]');
      if (tag === 'mobile') { await page.waitForTimeout(300); await page.click('[data-act="home"]') }
      await page.waitForSelector('.home .dash');
      await page.click('.dash [data-tool="compare"][data-rival="Lingva Test"]');
      await page.waitForSelector('.art-card:has-text("Сравнение с конкурентом")');
      const rq = calls.filter(c => c.path === '/api/quick' && c.body.company === 'Lingva Test');
      assert(rq.length === 3 && rq.every((c, i) => c.body.i === i && c.body.niche === 'курсы английского'), `${tag} compare: the same 3 questions for the competitor via /api/quick`);
      if (tag === 'mobile') await page.click('.art-card:has-text("Сравнение с конкурентом")');
      await page.waitForSelector('.panel .cmp', { state: 'visible' });
      assert((await page.textContent('.panel .stat')).includes('1 : 3'), `${tag} compare: both measurements side by side`);
      await page.waitForTimeout(400);
      await noHScroll(page, `${tag} compare`);
      await shot(page, `${tag}-15-compare.png`);
      await ctx.close();
    }

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
    await shot(page, 'desktop-10-server-down.png');
    down = false;
    await page.click('[data-retry]');
    await page.waitForSelector('.art-card');
    assert(true, 'retry works once the server is back');
    assert(downErrors.every(e => /Failed to load resource|ERR_CONNECTION_REFUSED|ERR_FAILED/.test(e)), 'only network errors from the stubbed outage');
    await ctx.close();

    /* ---------- old cabinet address redirects to the new one, keeping ?src= and ?company= ---------- */
    ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    page = await ctx.newPage(); watch(page, errors);
    await stub(page);
    await page.goto(`http://127.0.0.1:${port}/korin/app/index.html?src=threads&company=${encodeURIComponent('Hello Kids')}`);
    await page.waitForURL(/\/korin\/ai\//);
    const u = new URL(page.url());
    assert(u.pathname === '/korin/ai/' && u.searchParams.get('src') === 'threads' && u.searchParams.get('company') === 'Hello Kids', 'korin/app → korin/ai with the query string');
    await page.waitForSelector('.item:has-text("Hello Kids")');
    assert(await page.evaluate(() => sessionStorage.getItem('korin-src')) === 'threads', '?src= label is kept for the visit');
    await ctx.close();
    const old = fs.readFileSync(path.join(ROOT, 'korin/app/index.html'), 'utf8'), land = fs.readFileSync(path.join(ROOT, 'korin/index.html'), 'utf8');
    assert(/<noscript>[^]*href="\.\.\/ai\/"/.test(old) && old.length < 2000, 'korin/app is a tiny redirect with a <noscript> link');
    assert(!/app\/index\.html/.test(land) && (land.match(/href="ai\/"/g) || []).length >= 2 && land.includes(`a[href^="ai/"]`), 'every cabinet link on the landing points to ai/ (with withSrc)');

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
    await page.click('[data-act="cn-tg"]');
    await page.waitForSelector('#cnForm');
    await page.fill('#cnForm [name=token]', TOKEN); await page.fill('#cnForm [name=chat]', 'https://t.me/akku_channel');
    await shot(page, 'mobile-6-telegram-connect.png');
    await page.click('#cnGo');
    await page.waitForSelector('.cn-card[data-cn="tg"] .tag.ok');
    await page.click('.top [data-act="menu"]');
    await page.waitForTimeout(300);
    await page.click('.item[data-chat]');
    await page.fill('#inp', 'напиши пост для телеграм-канала'); await page.press('#inp', 'Enter');
    await page.waitForSelector('.art-card >> nth=1');
    await page.locator('.art-card').last().click();
    await page.waitForSelector('.panel [data-act="pub"]');
    await page.waitForTimeout(350);
    await noHScroll(page, 'mobile draft');
    await page.click('.panel [data-act="pub"]');
    await page.waitForSelector('#pubForm');
    await page.waitForTimeout(200);
    await shot(page, 'mobile-7-publish-preview.png');
    await page.click('#pubGo');
    await page.waitForSelector('.d-ok');
    await shot(page, 'mobile-8-published.png');
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
