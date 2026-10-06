const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');
(async () => {
  const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new', args: ['--no-sandbox','--use-gl=swiftshader','--enable-unsafe-swiftshader'] });
  const p = await b.newPage();
  await p.setViewport({ width: 1920, height: 1080 });
  await p.goto('http://127.0.0.1:8000/', { waitUntil: 'networkidle2' });
  await new Promise(r => setTimeout(r, 6000));
  const d = await p.evaluate(() => window.__sandbox ? window.__sandbox.debug() : 'no sandbox');
  console.log(JSON.stringify(d, null, 1));
  await b.close();
})();
