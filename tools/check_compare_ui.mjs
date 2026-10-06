// 对比功能（原图对照）的交互检查：驱动真实浏览器 + 真实鼠标事件。
//
// 覆盖三个用户报告的问题：
//   1. 擦除时在图上拖动，会把「成品预览」当成新素材叠到画布上（原生图片拖放被 drop 处理器收下）
//   2. 点「按住」看原图没反应（首次按下时模式还没激活，按下被忽略）
//   3. 「闪烁」这个功能用户不需要（应当已被移除）
//
// 用法：node tools/check_compare_ui.mjs --base http://127.0.0.1:8797 [--shot <png>]
import { launch, reporter } from './cdp.mjs';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = arg('--base', 'http://127.0.0.1:8797');
const SHOT = arg('--shot', '');
const HERE = dirname(fileURLToPath(import.meta.url));
const SAMPLE = join(HERE, '..', '.verify', 'cr3_preview.jpg');   // 真实照片，供服务端打开

const R = reporter('对比功能交互检查');

async function api(path, body) {
  const r = await fetch(BASE + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return r.json();
}

async function main() {
  // 准备一个"已编辑"的工程：对比控件只在有调色类修改时出现
  const opened = await api('/api/open', { paths: [SAMPLE], mode: 'new', name: 'ui对比检查' });
  if (!opened.project) throw new Error('无法准备工程：' + JSON.stringify(opened).slice(0, 200));
  await api('/api/ops', { ops: [{ op: 'exposure', value: 0.35, layer: 'L1' },
                                { op: 'saturation', value: 20, layer: 'L1' }] });
  const before = await api('/api/state');
  const nLayers0 = before.project.layers.length;
  const nAssets0 = before.project.assets.length;

  const s = await launch({ cdpPort: Number(arg('--cdp', '9334')) });
  try {
    R.check(await s.goto(BASE + '/'), '页面已加载');
    await s.sleep(900);
    R.check(await s.evaluate('!!(ST && ST.project && ST.project.edited)'),
      `工程处于"已编辑"状态（对比控件应可见）`);

    // ---- 问题 3：闪烁功能应当已被移除
    R.check(await s.evaluate('!document.querySelector("#cmpBlink")'), '「闪烁」按钮已移除');
    R.check(!readFileSync(join(HERE, '..', 'cogitator', 'web', 'app.js'), 'utf8').includes('blink'),
      'app.js 里已无 blink 相关代码');

    // ---- 问题 1：擦除模式下拖动，不得把成品叠加成新素材
    const wipeBtn = await s.centerOf('#cmpWipe');
    R.check(!!wipeBtn, '找到「擦除」按钮');
    await s.click(wipeBtn.x, wipeBtn.y);
    await s.sleep(300);
    R.check(await s.evaluate('CMP.mode === "wipe"'), '已进入擦除模式');

    const stack = await s.centerOf('#stack');
    const split0 = await s.evaluate('CMP.split');
    // 在图上横向拖动（真实鼠标事件，可能触发原生图片拖放）
    await s.drag({ x: stack.left + 40, y: stack.top + Math.round(stack.height * 0.4) },
                 { x: stack.left + stack.width - 40, y: stack.top + Math.round(stack.height * 0.4) });
    await s.sleep(600);
    const split1 = await s.evaluate('CMP.split');
    R.check(split1 !== split0, `拖动分割线生效（${split0.toFixed(2)} → ${split1.toFixed(2)}）`);

    const after = await api('/api/state');
    R.check(after.project.layers.length === nLayers0,
      `拖动后图层数不变（${nLayers0} → ${after.project.layers.length}）—— 不应把成品叠上去`);
    R.check(after.project.assets.length === nAssets0,
      `拖动后素材数不变（${nAssets0} → ${after.project.assets.length}）`);
    const ids0 = before.project.assets.map((a) => a.id).sort().join(',');
    const ids1 = after.project.assets.map((a) => a.id).sort().join(',');
    R.check(ids0 === ids1, `素材集合完全未变（${ids1}）`);
    R.check(await s.evaluate('document.querySelector("#preview").draggable === false'),
      '预览图已标记为不可拖拽');
    R.check(await s.evaluate('document.querySelector("#previewOrig").draggable === false'),
      '原图基准也已标记为不可拖拽');

    // ---- 问题 2：「按住」必须首次按下就立即显示原图（关键：必须在"未进入任何对比模式"下测，
    //      否则擦除模式本来就让原图可见，断言会因为错误的原因通过）
    await s.evaluate('CMP.mode = "off"; CMP.hold = false; applyCompare();');
    await s.sleep(200);
    R.check(await s.evaluate('document.querySelector("#previewOrig").classList.contains("hidden")'),
      '基准状态：未进入对比模式时原图不可见');
    const holdBtn = await s.centerOf('#cmpHold');
    R.check(!!holdBtn, '找到「按住」按钮');
    await s.mouse('mouseMoved', holdBtn.x, holdBtn.y, { buttons: 0 });
    await s.mouse('mousePressed', holdBtn.x, holdBtn.y);
    await s.sleep(250);
    const shownOnPress = await s.evaluate(`(() => {
      const o = document.querySelector('#previewOrig');
      return !o.classList.contains('hidden') && o.style.opacity !== '0';
    })()`);
    R.check(shownOnPress, '「按住」首次按下即显示原图（无需先切换模式）');
    if (SHOT) { await s.screenshot(SHOT); console.log(`  （截图已保存：${SHOT}）`); }
    await s.mouse('mouseReleased', holdBtn.x, holdBtn.y, { buttons: 0 });
    await s.sleep(250);
    R.check(await s.evaluate('document.querySelector("#previewOrig").classList.contains("hidden")'),
      '松开后原图隐藏（恢复看成品）');

    // 键盘 \ 也应即时生效
    await s.send('Input.dispatchKeyEvent', { type: 'keyDown', key: '\\', code: 'Backslash',
                                             windowsVirtualKeyCode: 220 });
    await s.sleep(200);
    R.check(await s.evaluate('!document.querySelector("#previewOrig").classList.contains("hidden")'),
      '按住 \\ 键同样立即显示原图');
    await s.send('Input.dispatchKeyEvent', { type: 'keyUp', key: '\\', code: 'Backslash',
                                             windowsVirtualKeyCode: 220 });
    await s.sleep(200);
    R.check(await s.evaluate('document.querySelector("#previewOrig").classList.contains("hidden")'),
      '松开 \\ 键恢复');
  } finally {
    await s.close();
  }
  return R.finish();
}

main().then((code) => process.exit(code))
  .catch((e) => { console.error('检查脚本出错：', e.message); process.exit(2); });
