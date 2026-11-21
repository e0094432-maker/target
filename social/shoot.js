// The marketer's camera: opens the live Körin site, takes one screenshot or a short video of a product screen.
// Usage: node social/shoot.js <base url> <out dir> [scene number]. Prints one JSON line: {file, kind, shows}.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [base = 'https://kenesai.asia/korin/', out = 'korin/media', pick] = process.argv.slice(2);
// smooth scrolling on the page would leave the camera halfway, so jump at once
const jump = (p, sel, gap) => p.evaluate(([sel, gap]) => { const el = document.querySelector(sel); scrollTo({ top: el.getBoundingClientRect().top + scrollY - gap, behavior: 'instant' }) }, [sel, gap]);
const SCENES = [
  { id: 'hero', kind: 'video', url: '', shows: 'первый экран сайта Körin: клиент спрашивает нейросеть про курсы английского в Алматы, она называет три школы, а компании клиента в ответе нет; справа цифра «вас назвали 11 из 30» и три дела на неделю. Это пример на вымышленной школе', run: async p => { await p.waitForTimeout(11000) } },
  { id: 'cards', kind: 'image', url: '', shows: 'карточки возможностей Körin: ответ нейросети целиком, простые цифры «3 из 10», проверка сайта (видит ли его ChatGPT, Gemini, Perplexity, Claude), три дела на неделю, вопросы на русском и казахском', run: async p => { await jump(p, '.bento', 150); await p.waitForTimeout(2500) } },
  { id: 'streams', kind: 'video', url: '', shows: 'экран с надписью «Нейросеть читает сайты и по ним отвечает. Körin покажет, есть ли там вы»; значки площадок (карты, Instagram, YouTube, Википедия) летят к центру', run: async p => { await jump(p, '#web', -260); await p.waitForTimeout(8000) } },
  { id: 'cabinet', kind: 'image', url: 'app/index.html', shows: 'кабинет Körin, экран «Обзор»: вас назвали 11 раз из 30, список «кого называют» с конкурентами, блок «что сделать сейчас» с тремя делами. Это пример на вымышленной школе Hello Kids', run: async p => { await p.fill('#o1', 'Hello Kids'); await p.fill('#o2', 'курсы английского'); await p.click('#next'); await p.fill('#o4', 'English Hub'); await p.click('#addr'); await p.click('#next'); await p.click('#run'); await p.waitForTimeout(2500) }, example: true },
  { id: 'helpers', kind: 'image', url: '', shows: 'блок сайта «ИИ-помощники делают работу за вас»: проверяют сайт, пишут тексты, просят отзывы; вокруг названия Körin семь помощников', run: async p => { await jump(p, '#agents', 40); await p.waitForTimeout(1800) } },
  { id: 'light', kind: 'image', url: '', shows: 'первый экран сайта Körin на светлом фоне: «Узнайте, кого ChatGPT советует вместо вас», поле «название вашей компании» и кнопка «Проверить бизнес». Проверка бесплатная и занимает минуту', run: async p => { await p.click('#themeBtn'); await p.waitForTimeout(9500) } },
];

(async () => {
  const day = Math.floor(Date.now() / 864e5), scene = SCENES[(pick !== undefined ? +pick : day) % SCENES.length];
  const stamp = new Date().toISOString().slice(0, 10), name = `${stamp}-${scene.id}`;
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch();
  const video = scene.kind === 'video';
  const ctx = await browser.newContext({ viewport: { width: 1080, height: 1350 }, locale: 'ru-RU', recordVideo: video ? { dir: out, size: { width: 1080, height: 1350 } } : undefined });
  const page = await ctx.newPage();
  // the example cabinet must not talk to the server: the screenshot shows the labelled example
  if (scene.example) await page.route('**/api/**', r => r.abort());
  await page.goto(new URL(scene.url, base).href, { waitUntil: 'networkidle' }).catch(() => {});
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelectorAll('.rv').forEach(x => x.classList.add('in', 'on', 'show', 'vis'))).catch(() => {});
  await scene.run(page);
  let file = `${name}.png`;
  if (video) { const v = page.video(); await ctx.close(); fs.renameSync(await v.path(), path.join(out, `${name}.webm`)); file = `${name}.webm` }
  else { await page.screenshot({ path: path.join(out, file) }); await ctx.close() }
  await browser.close();
  console.log(JSON.stringify({ file, kind: scene.kind, shows: scene.shows }));
})();
