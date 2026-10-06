/* 三维沙盘专项检查：画布尺寸、WebGL 上下文、非空像素比例。 */
const puppeteer = require('C:/Users/Administrator/AppData/Local/Temp/pptr-check/node_modules/puppeteer-core');
const fs = require('fs');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-sandbox', '--use-gl=swiftshader', '--enable-unsafe-swiftshader'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080 });
  // ?capture=1 让渲染器保留绘制缓冲，便于用 readPixels 校验画布像素
  await page.goto('http://127.0.0.1:8000/?capture=1', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await new Promise((r) => setTimeout(r, 6000));

  const info = await page.evaluate(() => {
    const canvas = document.querySelector('#sandbox canvas');
    if (!canvas) return { found: false };
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    const box = canvas.getBoundingClientRect();
    return {
      found: true,
      attr: { w: canvas.width, h: canvas.height },
      css: { w: Math.round(box.width), h: Math.round(box.height) },
      hasContext: !!gl,
      renderer: gl ? gl.getParameter(gl.VERSION) : null,
      childGroups: document.querySelectorAll('#sandbox canvas').length,
    };
  });

  // 对沙盘区域单独截图并统计非背景像素
  const el = await page.$('#sandbox');
  await el.screenshot({ path: 'D:/front-back/OpcOcean/tools/sandbox.png' });

  const stats = await page.evaluate(async () => {
    const canvas = document.querySelector('#sandbox canvas');
    if (!canvas) return null;
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) return null;
    const w = canvas.width, h = canvas.height;
    const px = new Uint8Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
    let nonEmpty = 0;
    for (let i = 0; i < px.length; i += 4) {
      if (px[i] + px[i + 1] + px[i + 2] > 24) nonEmpty += 1;
    }
    return { total: w * h, nonEmpty, ratio: +(nonEmpty / (w * h)).toFixed(4) };
  });

  await browser.close();
  console.log(JSON.stringify({ info, stats, sandboxPng: fs.existsSync('D:/front-back/OpcOcean/tools/sandbox.png') }, null, 2));
})();
