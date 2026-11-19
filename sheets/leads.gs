// Принимает заявки с сайта kenesai.asia и дописывает их строкой в эту таблицу.
// Установка: в таблице открыть «Расширения → Apps Script», вставить этот код,
// «Начать развертывание → Новое развертывание → Веб-приложение»,
// «Запуск от имени: Я», «У кого есть доступ: Все». Полученный URL вписать в CONFIG.sheet в index.html.
const HEADERS = ['Дата', 'Имя', 'Бизнес или сайт', 'WhatsApp', 'Язык'];

function doPost(e) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
  if (sheet.getLastRow() === 0) sheet.appendRow(HEADERS);
  const d = JSON.parse(e.postData.contents);
  const clean = v => String(v || '').slice(0, 300).replace(/^[=+\-@]/, "'$&"); // не даём записать формулу
  sheet.appendRow([new Date(), clean(d.name), clean(d.business), clean(d.whatsapp), clean(d.lang)]);
  return ContentService.createTextOutput('ok');
}
