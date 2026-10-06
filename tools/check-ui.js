/* 大屏与控制台页面的渲染校验脚本（Puppeteer） */
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';

async function run() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });

  const results = [];

  for (const shot of [
    { name: 'screen-1920', url: `${BASE}/`, w: 1920, h: 1080, wait: 5200 },
    { name: 'screen-1280', url: `${BASE}/`, w: 1280, h: 720, wait: 4200 },
    { name: 'console-1440', url: `${BASE}/console`, w: 1440, h: 960, wait: 2600 },
  ]) {
    const page = await browser.newPage();
    const errors = [];
    const warnings = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text());
      if (msg.type() === 'warning') warnings.push(msg.text());
    });
    page.on('pageerror', (err) => errors.push('pageerror: ' + err.message));
    page.on('requestfailed', (req) => errors.push('requestfailed: ' + req.url() + ' ' + (req.failure() || {}).errorText));

    await page.setViewport({ width: shot.w, height: shot.h, deviceScaleFactor: 1 });
    await page.goto(shot.url, { waitUntil: 'domcontentloaded', timeout: 60000 });
    if (shot.name.startsWith('console')) {
      await page.evaluate(async () => {
        const res = await fetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: 'admin', password: 'deepblue@2026' }),
        });
        const data = await res.json();
        localStorage.setItem('deepblue.token', data.access_token);
        localStorage.setItem('deepblue.user', JSON.stringify(data.user));
      });
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    }
    await new Promise((r) => setTimeout(r, shot.wait));

    const info = await page.evaluate(() => {
      const screen = document.getElementById('screen');
      const rect = screen ? screen.getBoundingClientRect() : null;
      const canvases = Array.from(document.querySelectorAll('canvas')).map((c) => ({
        w: c.width, h: c.height, cls: c.className || '',
      }));
      const bodyOverflowX = document.documentElement.scrollWidth - document.documentElement.clientWidth;
      const texts = document.body.innerText.replace(/\s+/g, ' ').slice(0, 400);
      return {
        title: document.title,
        screenRect: rect ? { w: Math.round(rect.width), h: Math.round(rect.height) } : null,
        canvasCount: canvases.length,
        canvases,
        overflowX: bodyOverflowX,
        hasNoData: /undefined|NaN|Infinity/.test(texts),
        texts,
      };
    });

    await page.screenshot({ path: `D:/front-back/OpcOcean/tools/${shot.name}.png`, fullPage: false });
    results.push({ name: shot.name, errors, warnings: warnings.slice(0, 5), info });
    await page.close();
  }

  await browser.close();
  console.log(JSON.stringify(results, null, 2));
}

run().catch((err) => { console.error('FATAL', err); process.exit(1); });
