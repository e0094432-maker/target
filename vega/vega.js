/* Vega Legal: заявки и визиты пишутся в ту же Google Таблицу, что у Kenes (sheets/leads.gs).
   Без JavaScript страница читается целиком; скрипт только отправляет форму и отмечает визиты. */
(() => {
  const SHEET = 'https://script.google.com/macros/s/AKfycbymjjOb_07W6QFm-vZu7EpVT9kvzA8WbfzVFvVA4UfU5bKlHRZ3G1pCIDZ2kY0nP1CG/exec';
  const LANG = (document.documentElement.lang || 'ru').slice(0, 2);
  const T = {
    ru: { need: 'Заполните имя, компанию и WhatsApp.', phone: 'Напишите номер WhatsApp полностью, например +7 701 234 56 78.', ok: 'Спасибо! Юрист Vega Legal напишет вам в WhatsApp. Если сообщения не будет, напишите нам сами: +7 702 168 67 33.', fail: 'Не получилось отправить. Напишите нам в WhatsApp: +7 702 168 67 33.' },
    kk: { need: 'Атыңызды, компанияны және WhatsApp нөмірін жазыңыз.', phone: 'WhatsApp нөмірін толық жазыңыз, мысалы +7 701 234 56 78.', ok: 'Рақмет! Vega Legal заңгері сізге WhatsApp арқылы жазады. Хабар келмесе, өзіңіз жазыңыз: +7 702 168 67 33.', fail: 'Жіберу мүмкін болмады. WhatsApp-қа жазыңыз: +7 702 168 67 33.' },
    en: { need: 'Please fill in your name, company and WhatsApp.', phone: 'Please enter the full WhatsApp number, e.g. +7 701 234 56 78.', ok: 'Thank you! A Vega Legal lawyer will message you on WhatsApp. If you hear nothing, write to us: +7 702 168 67 33.', fail: 'Could not send. Please message us on WhatsApp: +7 702 168 67 33.' }
  }[LANG] || {};

  /* номер посетителя общий для всего сайта (как на Kenes и Körin), метка источника живёт до конца визита */
  let vid = null; try { vid = localStorage.getItem('vid'); if (!vid) { vid = Math.random().toString(36).slice(2, 7).toUpperCase(); localStorage.setItem('vid', vid) } } catch (e) { vid = 'без номера' }
  const P = new URLSearchParams(location.search);
  let src = (P.get('utm_source') || P.get('src') || (document.referrer && !document.referrer.includes(location.hostname) ? new URL(document.referrer).hostname : '')).slice(0, 40);
  try { if (src) sessionStorage.setItem('vega-src', src); else src = sessionStorage.getItem('vega-src') || '' } catch (e) {}
  src = src || 'прямой заход';
  const page = location.pathname.replace(/^\/vega\/?/, '/') || '/';
  const dev = /Mobi|Android|iPhone/i.test(navigator.userAgent) ? 'телефон' : 'компьютер', t0 = Date.now();
  let deep = 0, left = false;
  addEventListener('scroll', () => { deep = Math.max(deep, Math.round((scrollY + innerHeight) / document.documentElement.scrollHeight * 100)) }, { passive: true });

  const track = (what, beacon) => {
    if (navigator.webdriver) return;  // автотесты и боты-браузеры не засоряют таблицу
    const body = JSON.stringify({ name: 'Vega: ' + what, business: 'откуда: ' + src + ' · страница ' + page, whatsapp: 'посетитель ' + vid + ' · ' + dev, lang: 'vega' });
    if (beacon && navigator.sendBeacon) navigator.sendBeacon(SHEET, body); else fetch(SHEET, { method: 'POST', mode: 'no-cors', body, keepalive: true }).catch(() => {});
  };
  let seen = false; try { seen = sessionStorage.getItem('seen-vega' + page); sessionStorage.setItem('seen-vega' + page, 1) } catch (e) {}
  if (!seen) track('зашёл');
  addEventListener('pagehide', () => { if (!left) { left = true; track('ушёл: был ' + Math.round((Date.now() - t0) / 1000) + ' сек, пролистал ' + deep + '%', true) } });
  document.querySelectorAll('a[href^="https://wa.me/"]').forEach(a => a.addEventListener('click', () => track('нажал «WhatsApp»')));

  /* плавающая кнопка WhatsApp на телефоне появляется, когда кнопки первого экрана и формы не видны */
  const fl = document.querySelector('.wa-float'), watch = document.querySelectorAll('.hero .btns, #contact');
  if (fl && watch.length && 'IntersectionObserver' in window) {
    const vis = new Set();
    const io = new IntersectionObserver(es => { es.forEach(e => e.isIntersecting ? vis.add(e.target) : vis.delete(e.target)); fl.classList.toggle('on', !vis.size) });
    watch.forEach(el => io.observe(el));
  }

  /* шапка темнеет после начала прокрутки; меню на телефоне закрывается после выбора пункта */
  const top = document.querySelector('.top'), mt = document.getElementById('menu-t');
  if (top) { const sc = () => top.classList.toggle('s', scrollY > 40); sc(); addEventListener('scroll', sc, { passive: true }) }
  if (mt) document.querySelectorAll('.nav a').forEach(a => a.addEventListener('click', () => { mt.checked = false }));

  const f = document.getElementById('leadForm'); if (!f) return;
  const msg = f.querySelector('.msg');
  let began = false; f.addEventListener('input', () => { if (!began) { began = true; track('начал заполнять форму') } });
  f.addEventListener('submit', async e => {
    e.preventDefault();
    const name = f.name.value.trim(), company = f.company.value.trim(), wa = f.whatsapp.value.trim();
    if (!name || !company || !wa) { msg.textContent = T.need; return }
    if (wa.replace(/\D/g, '').length < 10) { msg.textContent = T.phone; return }
    if (f.dataset.busy) return;  // повторные нажатия не создают дубли
    f.dataset.busy = '1'; const btn = f.querySelector('button'); btn.disabled = true; msg.textContent = '…';
    /* text/plain без своих заголовков: Apps Script не принимает предварительный запрос; ответ no-cors не прочитать */
    const data = { name: 'Vega: ' + name, business: company, whatsapp: wa, lang: 'vega-' + LANG + ' · ' + src + ' · посетитель ' + vid };
    try { await fetch(SHEET, { method: 'POST', mode: 'no-cors', body: JSON.stringify(data) }); f.reset(); msg.textContent = T.ok }
    catch (err) { msg.textContent = T.fail }
    finally { delete f.dataset.busy; btn.disabled = false }
  });
})();
