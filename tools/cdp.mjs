// 共享的浏览器驱动模块（headless Edge/Chrome via CDP，零依赖）。
//
// 为什么需要它：HTTP 层与像素层断言再多也碰不到按钮。交互缺陷（按钮没反应、
// 拖拽误触发）只能由真实浏览器来抓。Node ≥21 自带全局 WebSocket 与 fetch，
// 因此不需要 playwright/selenium。
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
];

export function pickBrowser(explicit = '') {
  if (explicit && existsSync(explicit)) return explicit;
  for (const p of CANDIDATES) if (existsSync(p)) return p;
  throw new Error('找不到 Edge/Chrome：' + CANDIDATES.join(' | '));
}

/** 启动浏览器并连上 CDP，返回一个会话对象。 */
export async function launch({ browser = '', cdpPort = 9333, width = 1280, height = 900 } = {}) {
  const exe = pickBrowser(browser);
  const profile = mkdtempSync(join(tmpdir(), 'cdp-'));
  const proc = spawn(exe, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-features=Translate', `--remote-debugging-port=${cdpPort}`,
    `--user-data-dir=${profile}`, `--window-size=${width},${height}`, 'about:blank',
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 80 && !target; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${cdpPort}/json/list`);
      target = (await r.json()).find((t) => t.type === 'page' && t.webSocketDebuggerUrl) || null;
    } catch { /* 还没起来 */ }
    if (!target) await sleep(250);
  }
  if (!target) { proc.kill(); throw new Error('浏览器调试端口没就绪'); }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  const pending = new Map();
  const events = [];
  let idc = 0;
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', () => rej(new Error('WebSocket 连接失败')));
  });
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    else if (m.method) events.push(m);
  });

  const send = (method, params = {}) => new Promise((res) => {
    const id = ++idc;
    pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params }));
  });

  const session = {
    events,
    send,
    sleep,
    async evaluate(expression, awaitPromise = false) {
      const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
      if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.text + ' :: ' + expression);
      return r.result?.result?.value;
    },
    /** 派发真实鼠标事件（Chrome 会据此合成 pointer 事件，也会触发原生拖放） */
    mouse(type, x, y, { button = 'left', buttons = 1, clickCount = 1 } = {}) {
      return send('Input.dispatchMouseEvent', { type, x, y, button, buttons, clickCount });
    },
    async click(x, y) {
      await session.mouse('mouseMoved', x, y, { buttons: 0 });
      await session.mouse('mousePressed', x, y);
      await sleep(30);
      await session.mouse('mouseReleased', x, y, { buttons: 0 });
    },
    /** 按住某点缓慢拖动一段距离（用于复现拖放/擦除分割线） */
    async drag(from, to, steps = 12) {
      await session.mouse('mouseMoved', from.x, from.y, { buttons: 0 });
      await session.mouse('mousePressed', from.x, from.y);
      for (let i = 1; i <= steps; i++) {
        const t = i / steps;
        await session.mouse('mouseMoved', from.x + (to.x - from.x) * t,
                            from.y + (to.y - from.y) * t, { buttons: 1 });
        await sleep(20);
      }
      await session.mouse('mouseReleased', to.x, to.y, { buttons: 0 });
    },
    centerOf(sel) {
      return session.evaluate(`(() => {
        const el = document.querySelector(${JSON.stringify(sel)});
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2),
                 left: Math.round(r.left), top: Math.round(r.top),
                 width: Math.round(r.width), height: Math.round(r.height) };
      })()`);
    },
    async goto(url, waitFor = '#btnQuit') {
      await send('Page.navigate', { url });
      for (let i = 0; i < 90; i++) {
        if (await session.evaluate(`!!document.querySelector(${JSON.stringify(waitFor)})`)) return true;
        await sleep(150);
      }
      return false;
    },
    async screenshot(path) {
      const png = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path, Buffer.from(png.result.data, 'base64'));
      return path;
    },
    async close() {
      try { ws.close(); } catch { /* ignore */ }
      try { proc.kill(); } catch { /* ignore */ }
      await sleep(400);
      try { rmSync(profile, { recursive: true, force: true }); } catch { /* Windows 可能占用 */ }
    },
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Network.enable');
  return session;
}

/** 简易断言收集器 */
export function reporter(title) {
  const results = [];
  return {
    results,
    check(ok, label) {
      results.push({ ok: !!ok, label });
      console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${label}`);
    },
    finish() {
      const bad = results.filter((r) => !r.ok);
      console.log(`\n${title}：通过 ${results.length - bad.length} 项，失败 ${bad.length} 项`);
      return bad.length ? 1 : 0;
    },
  };
}
