/* 渲染演示海报：抓取实时界面截图 + 输出 9 张 1920x1080 海报 PNG */
const fs = require('fs');
const path = require('path');
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = 'http://127.0.0.1:8000';
const ROOT = 'D:/front-back/OpcOcean/docs/posters';
const ASSETS = path.join(ROOT, 'assets');
const OUT = path.join(ROOT, 'out');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login(page) {
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
  await sleep(2600);
}

async function clickConsoleView(page, label) {
  await page.evaluate((name) => {
    const btn = Array.from(document.querySelectorAll('.top-nav')).find((b) => b.textContent.replace(/\s+/g, '').includes(name));
    if (btn) btn.click();
  }, label);
  await sleep(2400);
}

(async () => {
  fs.mkdirSync(ASSETS, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });

  // 1) 大屏截图
  const screenPage = await browser.newPage();
  await screenPage.setCacheEnabled(false);
  await screenPage.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await screenPage.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(6200);
  await screenPage.screenshot({ path: path.join(ASSETS, 'screen.png') });
  await screenPage.close();

  // 2) 后台截图
  const consolePage = await browser.newPage();
  await consolePage.setCacheEnabled(false);
  await consolePage.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await login(consolePage);
  await clickConsoleView(consolePage, '设备管理');
  await consolePage.screenshot({ path: path.join(ASSETS, 'console-devices.png') });
  await clickConsoleView(consolePage, '赤潮模型');
  await consolePage.screenshot({ path: path.join(ASSETS, 'console-model.png') });
  await clickConsoleView(consolePage, '红树林监测');
  await consolePage.screenshot({ path: path.join(ASSETS, 'console-mangrove.png') });
  await consolePage.close();

  // 3) 海报
  const deck = await browser.newPage();
  await deck.setCacheEnabled(false);
  await deck.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await deck.goto(`file:///${ROOT}/deck.html`, { waitUntil: 'load', timeout: 60000 });
  await deck.evaluate(async () => {
    const imgs = Array.from(document.images).filter((i) => !i.complete);
    await Promise.all(imgs.map((i) => new Promise((res) => { i.onload = i.onerror = res; })));
    if (document.fonts) await document.fonts.ready;
  });
  await sleep(900);

  const ids = await deck.$$eval('.poster', (els) => els.map((e) => e.id));
  const names = {
    p0: '01-主视觉封面', p1: '02-背景与痛点', p2: '03-感知层浮标', p3: '04-通信与边缘',
    p4: '05-数字孪生大屏', p5: '06-AI赤潮预警', p6: '07-红树林遥感', p7: '08-管理后台', p8: '09-成果与价值',
  };
  const overflow = [];
  for (const id of ids) {
    const el = await deck.$(`#${id}`);
    const box = await el.boundingBox();
    const inner = await deck.evaluate((i) => {
      const n = document.getElementById(i);
      return { sh: n.scrollHeight, ch: n.clientHeight, sw: n.scrollWidth, cw: n.clientWidth };
    }, id);
    if (inner.sh > inner.ch + 2 || inner.sw > inner.cw + 2) overflow.push({ id, ...inner });
    const file = path.join(OUT, `${names[id]}.png`);
    await el.screenshot({ path: file });
    console.log(`${names[id]}  ${Math.round(box.width)}x${Math.round(box.height)}  overflow=${inner.sh - inner.ch}/${inner.sw - inner.cw}`);
  }
  console.log('OVERFLOW', JSON.stringify(overflow));
  await browser.close();
  console.log('rendered', ids.length);
})();
