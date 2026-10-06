/* 视频 AI 识别页面的端到端校验：登录 → 进入页面 → 执行一次真实识别 → 截图。 */
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');
const path = require('path');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';
const OUT = process.env.OUT_DIR || path.join(__dirname);

async function clickByText(page, selector, text) {
  const ok = await page.evaluate((sel, want) => {
    const nodes = [...document.querySelectorAll(sel)];
    const hit = nodes.find((n) => (n.innerText || '').replace(/\s+/g, '').includes(want));
    if (!hit) return false;
    hit.click();
    return true;
  }, selector, text);
  if (!ok) throw new Error(`未找到包含「${text}」的 ${selector}`);
}

async function run() {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('requestfailed', (r) => errors.push('requestfailed: ' + r.url()));
  page.on('response', (r) => { if (r.status() >= 400) errors.push(`http ${r.status()} ${r.url()}`); });

  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 1 });
  await page.goto(`${BASE}/console`, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#console', { timeout: 20000 });

  // 登录页默认已填好演示账号，直接提交
  const loginVisible = await page.$('.login-card');
  if (loginVisible) {
    await page.click('.login-card button[type="submit"]');
    await page.waitForSelector('.console-shell', { timeout: 30000 });
  }
  await new Promise((r) => setTimeout(r, 1800));

  await clickByText(page, '.top-nav', '视频AI识别');
  await new Promise((r) => setTimeout(r, 1500));

  const statusText = await page.evaluate(() => document.body.innerText);
  const hasPanel = statusText.includes('视觉推理节点') && statusText.includes('巡视输入');

  // 执行一次真实识别
  await clickByText(page, 'button', '识别样本影像');
  await page.waitForFunction(
    () => {
      const img = document.querySelector('img[alt="识别标注结果"]');
      return img && img.src && img.src.length > 5000;
    },
    { timeout: 120000 },
  );
  await new Promise((r) => setTimeout(r, 600));

  const shot = path.join(OUT, 'vision-console.png');
  await page.screenshot({ path: shot, fullPage: true });

  const summary = await page.evaluate(() => {
    const text = document.body.innerText.replace(/\s+/g, ' ');
    const m = text.match(/识别到[^ ]{0,60}/);
    const rows = [...document.querySelectorAll('table.data tbody tr')].length;
    return { summary: m ? m[0] : null, rows };
  });

  await browser.close();
  console.log(JSON.stringify({ hasPanel, shot, summary, errors }, null, 2));
}

run().catch((err) => { console.error('FAILED:', err.message); process.exit(1); });
