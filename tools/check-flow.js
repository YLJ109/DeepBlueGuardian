/* 交互流程校验：遍历大屏视图与大屏/后台的全部功能页，检查运行时错误并截图。 */
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';

async function run() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const report = { screenModes: [], consoleViews: [], errors: [] };

  // ---------- 大屏视图切换 ----------
  const screen = await browser.newPage();
  screen.on('pageerror', (e) => report.errors.push('screen: ' + e.message));
  screen.on('console', (m) => { if (m.type() === 'error') report.errors.push('screen console: ' + m.text()); });
  await screen.setViewport({ width: 1920, height: 1080 });
  await screen.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await new Promise((r) => setTimeout(r, 4500));

  const tabs = await screen.$$('.screen-nav button');
  for (let i = 0; i < tabs.length; i += 1) {
    await tabs[i].click();
    await new Promise((r) => setTimeout(r, 1400));
    const label = await screen.evaluate((idx) => document.querySelectorAll('.screen-nav button')[idx].textContent.trim(), i);
    const state = await screen.evaluate(() => {
      const canvas = document.querySelector('canvas');
      const gl = canvas ? canvas.getContext('webgl2') || canvas.getContext('webgl') : null;
      return {
        canvas: canvas ? { w: canvas.width, h: canvas.height } : null,
        hasContext: !!gl,
        legend: Array.from(document.querySelectorAll('.legend-row')).map((n) => n.textContent.trim()),
        banner: (document.querySelector('.ai-banner') || {}).textContent || '',
      };
    });
    report.screenModes.push({ mode: label, ...state });
    await screen.screenshot({ path: `D:/front-back/OpcOcean/tools/screen-mode-${i}.png` });
  }

  // ---------- 后台功能页 ----------
  const page = await browser.newPage();
  page.on('pageerror', (e) => report.errors.push('console: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') report.errors.push('console console: ' + m.text()); });
  await page.setViewport({ width: 1600, height: 1000 });
  await page.goto(`${BASE}/console`, { waitUntil: 'domcontentloaded', timeout: 60000 });
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
  await new Promise((r) => setTimeout(r, 2600));

  const navCount = await page.evaluate(() => document.querySelectorAll('.top-nav').length);
  for (let i = 0; i < navCount; i += 1) {
    await page.evaluate((idx) => document.querySelectorAll('.top-nav')[idx].click(), i);
    await new Promise((r) => setTimeout(r, 2600));
    const label = await page.evaluate((idx) => document.querySelectorAll('.top-nav')[idx].textContent.trim(), i);
    const state = await page.evaluate(() => ({
      canvases: document.querySelectorAll('canvas').length,
      rows: document.querySelectorAll('table.data tbody tr').length,
      navOverlap: (() => {
        const rects = [...document.querySelectorAll('.top-nav')].map((n) => n.getBoundingClientRect());
        let hits = 0;
        for (let a = 0; a < rects.length; a += 1) {
          for (let b = a + 1; b < rects.length; b += 1) {
            const overlapX = Math.min(rects[a].right, rects[b].right) - Math.max(rects[a].left, rects[b].left);
            const overlapY = Math.min(rects[a].bottom, rects[b].bottom) - Math.max(rects[a].top, rects[b].top);
            if (overlapX > 1 && overlapY > 1) hits += 1;
          }
        }
        return hits;
      })(),
      text: document.body.innerText.replace(/\s+/g, ' ').slice(0, 220),
      hasRawMustache: /\{\{/.test(document.body.innerText),
    }));
    report.consoleViews.push({ view: label, ...state });
    await page.screenshot({ path: `D:/front-back/OpcOcean/tools/console-view-${i}.png`, fullPage: false });
  }

  await browser.close();
  console.log(JSON.stringify(report, null, 2));
}

run().catch((e) => { console.error('FATAL', e); process.exit(1); });
