/* 移动巡护端（/m）端到端校验：
   登录 → 五个标签页遍历 → 横向溢出与元素溢出检查 → 截图。
   仅做真实页面校验，不修改任何业务数据。 */
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');
const path = require('path');
const fs = require('fs');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';
const OUT = process.env.OUT_DIR || path.join(__dirname, 'mobile-shots');

const VIEWPORTS = [
  { name: 'iphone-se', width: 375, height: 667, dsf: 2 },
  { name: 'iphone-x', width: 375, height: 812, dsf: 2 },
  { name: 'pixel-wide', width: 414, height: 896, dsf: 2 },
];

const TABS = [
  { key: 'home', label: '首页' },
  { key: 'patrol', label: '巡护' },
  { key: 'detect', label: '识别' },
  { key: 'alerts', label: '告警' },
  { key: 'profile', label: '我的' },
];

async function clickTab(page, label) {
  const ok = await page.evaluate((want) => {
    const nodes = [...document.querySelectorAll('.m-tab')];
    const hit = nodes.find((n) => (n.innerText || '').replace(/\s+/g, '').includes(want));
    if (!hit) return false;
    hit.click();
    return true;
  }, label);
  if (!ok) throw new Error(`未找到底部标签：${label}`);
}

async function overflowReport(page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    const horizontal = doc.scrollWidth - window.innerWidth;
    const offenders = [];
    const nodes = [...document.querySelectorAll('body *')];
    for (const el of nodes) {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      if (rect.right > window.innerWidth + 2 || rect.left < -2) {
        if (style.position === 'fixed') continue;
        let scrollable = false;
        for (let p = el.parentElement; p; p = p.parentElement) {
          const ps = getComputedStyle(p);
          if (['auto', 'scroll'].includes(ps.overflowX)) { scrollable = true; break; }
          if (p === document.body) break;
        }
        if (scrollable) continue;
        offenders.push({
          cls: (el.className || el.tagName).toString().slice(0, 60),
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 40),
        });
      }
    }
    const textClipped = [];
    for (const el of nodes) {
      if (el.children.length) continue;
      const style = getComputedStyle(el);
      if (style.display === 'none') continue;
      if (el.scrollWidth > el.clientWidth + 4 && el.clientWidth > 0 && !['auto', 'scroll'].includes(style.overflowX)) {
        textClipped.push({
          cls: (el.className || el.tagName).toString().slice(0, 60),
          text: (el.innerText || '').slice(0, 40),
          scroll: el.scrollWidth,
          client: el.clientWidth,
        });
      }
    }
    return { horizontal, offenders: offenders.slice(0, 8), textClipped: textClipped.slice(0, 8) };
  });
}

async function run() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });

  const failures = [];
  const summary = [];

  for (const vp of VIEWPORTS) {
    const page = await browser.newPage();
    const errors = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    page.on('requestfailed', (r) => {
      const url = r.url();
      if (url.includes('/ws')) return;
      errors.push('requestfailed: ' + url);
    });
    page.on('response', (r) => { if (r.status() >= 400) errors.push(`http ${r.status()} ${r.url()}`); });

    await page.setViewport({ width: vp.width, height: vp.height, deviceScaleFactor: vp.dsf, isMobile: true, hasTouch: true });
    await page.goto(`${BASE}/m`, { waitUntil: 'networkidle2', timeout: 60000 });

    const login = await page.$('.login-card');
    if (login) {
      await page.click('.login-card button[type="submit"]');
    }
    await page.waitForSelector('.m-shell', { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 1500));

    const perTab = [];
    for (const tab of TABS) {
      await clickTab(page, tab.label);
      await new Promise((r) => setTimeout(r, 1200));
      const report = await overflowReport(page);
      if (report.horizontal > 1) failures.push(`${vp.name}/${tab.key}: 页面横向溢出 ${report.horizontal}px`);
      if (report.offenders.length) failures.push(`${vp.name}/${tab.key}: 元素越界 ${JSON.stringify(report.offenders[0])}`);
      perTab.push({ tab: tab.key, horizontal: report.horizontal, offenders: report.offenders.length, textClipped: report.textClipped.length });
      await page.screenshot({ path: path.join(OUT, `${vp.name}-${tab.key}.png`) });

      // 在识别页实跑一次真实模型推理，确认结果渲染链路可用
      if (tab.key === 'detect' && vp.name === 'iphone-x') {
        const clicked = await page.evaluate(() => {
          const nodes = [...document.querySelectorAll('.m-view .m-chip')];
          const hit = nodes.find((n) => (n.innerText || '').includes('岸滩塑料瓶'));
          if (!hit) return false;
          hit.click();
          return true;
        });
        if (!clicked) failures.push('iphone-x/detect: 未找到内置样本「岸滩塑料瓶」');
        else {
          try {
            await page.waitForSelector('.m-shot img', { timeout: 30000 });
            await new Promise((r) => setTimeout(r, 800));
            const hits = await page.evaluate(() => document.querySelectorAll('.m-hit').length);
            if (!hits) failures.push('iphone-x/detect: 样本识别后未渲染任何目标');
            await page.screenshot({ path: path.join(OUT, `${vp.name}-detect-result.png`) });
          } catch (e) {
            failures.push('iphone-x/detect: 样本识别超时未返回结果');
          }
        }
      }
    }

    const tabbar = await page.evaluate(() => {
      const el = document.querySelector('.m-tabbar');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), buttons: el.querySelectorAll('.m-tab').length };
    });
    if (!tabbar || tabbar.buttons !== 5) failures.push(`${vp.name}: 底部标签数量异常 ${JSON.stringify(tabbar)}`);
    if (tabbar && tabbar.bottom > vp.height + 1) failures.push(`${vp.name}: 底部标签超出视口 bottom=${tabbar.bottom}`);

    if (errors.length) failures.push(`${vp.name}: 运行时错误 ${JSON.stringify(errors.slice(0, 5))}`);
    summary.push({ viewport: vp.name, tabbar, tabs: perTab, errors: errors.length });
    await page.close();
  }

  await browser.close();
  console.log(JSON.stringify({ ok: failures.length === 0, summary, failures }, null, 2));
  if (failures.length) process.exitCode = 1;
}

run().catch((err) => { console.error('CHECK_FAILED', err); process.exit(1); });
