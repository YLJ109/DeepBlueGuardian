/* 为 README 抓取运行中系统的界面截图（大屏 5 视图 + 智能体 + 后台 9 页 + 移动端）
 *
 * 前置：服务已启动（uvicorn app.main:app --port 8000），且本机具备 Chrome 与 puppeteer-core。
 * 用法：node tools/capture-screenshots.js
 * 输出：docs/screenshots/*.jpg（README「界面实拍」引用的原图）
 */
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');
const fs = require('fs');
const path = require('path');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';
const OUT = process.env.SHOT_DIR || path.resolve(__dirname, '..', 'docs', 'screenshots');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = (page, name) => page.screenshot({ path: path.join(OUT, name + '.jpg'), type: 'jpeg', quality: 86 });

const SCREEN_VIEWS = [
  ['总览', 'screen-overview'],
  ['水质监测', 'screen-water'],
  ['赤潮预警', 'screen-redtide'],
  ['红树林', 'screen-mangrove'],
  ['设备状态', 'screen-device'],
];

const CONSOLE_VIEWS = [
  ['运行概览', 'console-overview'],
  ['设备管理', 'console-devices'],
  ['历史数据', 'console-history'],
  ['告警记录', 'console-alerts'],
  ['巡护任务', 'console-patrol'],
  ['赤潮模型', 'console-model'],
  ['红树林监测', 'console-mangrove'],
  ['账号与审计', 'console-accounts'],
];

async function captureScreen(browser, notes) {
  const page = await browser.newPage();
  page.on('pageerror', (e) => notes.push('screen error: ' + e.message));
  await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(6500);

  const dismissAlert = async () => {
    await page.evaluate(() => {
      const btn = document.querySelector('.alert-modal-foot .btn.ghost');
      if (btn) btn.click();
    });
    await sleep(600);
  };
  await dismissAlert();

  for (const [label, name] of SCREEN_VIEWS) {
    const ok = await page.evaluate((text) => {
      const btn = Array.from(document.querySelectorAll('.screen-nav button')).find((b) => b.textContent.includes(text));
      if (!btn) return false;
      btn.click();
      return true;
    }, label);
    if (!ok) notes.push('missing screen nav: ' + label);
    await dismissAlert();
    await sleep(2200);
    await shot(page, name);
  }

  // 智能体：点开面板 → 触发推荐问法 → 等工具调用与结论落地
  await page.evaluate(() => document.querySelector('.top-agent')?.click());
  await sleep(1500);
  await page.evaluate(() => document.querySelector('.agent-chip')?.click());
  for (let i = 0; i < 40; i += 1) {
    const done = await page.evaluate(() => {
      const bubbles = Array.from(document.querySelectorAll('.agent-msg.assistant .bubble'));
      const last = bubbles[bubbles.length - 1];
      return !!(last && last.textContent.trim().length > 80);
    });
    if (done) break;
    await sleep(1500);
  }
  await sleep(900);
  await shot(page, 'screen-agent');
  await page.close();
}

async function login(page) {
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
}

async function clickNav(page, selector, title, notes, prefix) {
  const ok = await page.evaluate((sel, t) => {
    const btn = document.querySelector(`${sel}[title="${t}"]`);
    if (!btn) return false;
    btn.click();
    return true;
  }, selector, title);
  if (!ok) notes.push(`missing ${prefix} nav: ${title}`);
}

async function captureConsole(browser, notes) {
  const page = await browser.newPage();
  page.on('pageerror', (e) => notes.push('console error: ' + e.message));
  await page.setViewport({ width: 1920, height: 1200, deviceScaleFactor: 1 });
  await page.goto(`${BASE}/console`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await login(page);
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(3200);

  for (const [label, name] of CONSOLE_VIEWS) {
    await clickNav(page, '.top-nav', label, notes, 'console');
    await sleep(2600);
    await shot(page, name);
  }

  // 视频 AI 识别：选样本 → 真实推理 → 等结果渲染
  await clickNav(page, '.top-nav', '视频AI识别', notes, 'console');
  await sleep(2600);
  await page.evaluate(() => {
    const sel = document.querySelector('#vSample');
    if (!sel) return;
    const opt = Array.from(sel.options).find((o) => o.value);
    if (opt) {
      sel.value = opt.value;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  await sleep(900);
  await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button')).find((b) => b.textContent.includes('识别样本影像'));
    if (btn && !btn.disabled) btn.click();
  });
  for (let i = 0; i < 30; i += 1) {
    const done = await page.evaluate(() => {
      const text = document.body.innerText;
      return text.includes('检出') && !text.includes('识别中…');
    });
    if (done) break;
    await sleep(1500);
  }
  await sleep(1400);
  await shot(page, 'console-vision');
  await page.close();
}

async function captureMobile(browser, notes) {
  const page = await browser.newPage();
  page.on('pageerror', (e) => notes.push('mobile error: ' + e.message));
  await page.setViewport({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true });
  await page.goto(`${BASE}/m`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(5000);
  await shot(page, 'mobile-portal');
  await page.close();
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const notes = [];
  try {
    await captureScreen(browser, notes);
    await captureConsole(browser, notes);
    await captureMobile(browser, notes);
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ out: OUT, notes }, null, 2));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
