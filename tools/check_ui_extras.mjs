// 缩放视图 / 面板宽度 / 快捷键 / 导出记忆 的交互检查（真实浏览器 + 真实鼠标键盘事件）。
//
// 这些能力都有一个共同点：**只能由真实浏览器验证**（CSS 布局、localStorage、键盘事件、
// 滚动容器）。用 CDP 驱动，断言落在可观测的 DOM 状态上。
//
// 用法：node tools/check_ui_extras.mjs --base http://127.0.0.1:8830 [--shot <png>]
import { launch, reporter } from './cdp.mjs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = arg('--base', 'http://127.0.0.1:8830');
const SHOT = arg('--shot', '');
const HERE = dirname(fileURLToPath(import.meta.url));
const SAMPLE = join(HERE, '..', '.verify', 'cr3_preview.jpg');

const R = reporter('缩放/面板/快捷键/导出记忆 交互检查');

async function api(path, body) {
  const r = await fetch(BASE + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return r.json();
}

async function main() {
  const opened = await api('/api/open', { paths: [SAMPLE], mode: 'new', name: 'ui_extras' });
  if (!opened.project) throw new Error('无法准备工程：' + JSON.stringify(opened).slice(0, 160));
  const lid = opened.project.layers[0].id;
  // 先攒够 10 步，用于检验"长管线折叠"（阈值是 8 步）
  await api('/api/ops', { ops: [
    { op: 'exposure', value: 0.3, layer: lid },
    { op: 'contrast', value: 8, layer: lid },
    { op: 'saturation', value: 6, layer: lid },
    { op: 'vibrance', value: 5, layer: lid },
    { op: 'shadows', value: 10, layer: lid },
    { op: 'highlights', value: -8, layer: lid },
    { op: 'clarity', value: 6, layer: lid },
    { op: 'temperature', value: 5, layer: lid },
    { op: 'vignette', amount: 6, layer: lid },
    { op: 'sharpen', value: 20, layer: lid },
  ] });

  const s = await launch({ cdpPort: Number(arg('--cdp', '9340')) });
  try {
    R.check(await s.goto(BASE + '/', '#preview'), '页面已加载');
    await s.sleep(900);
    const natW = await s.evaluate('document.querySelector("#preview").naturalWidth');
    R.check(natW > 0, `预览已就绪（原始宽 ${natW}px）`);

    // ---- 1:1 检查视图
    const zBtn = await s.centerOf('#btnZoom');
    R.check(!!zBtn, '顶栏有缩放按钮');
    R.check(await s.evaluate('document.querySelector("#btnZoom").disabled === false'),
      '有工程时缩放按钮可用');
    await s.click(zBtn.x, zBtn.y);
    await s.sleep(400);
    const w100 = await s.evaluate('parseInt(document.querySelector("#stack").style.width, 10)');
    R.check(Math.abs(w100 - natW) <= 2, `预览 100% 下画布宽 = 预览像素宽（${w100} vs ${natW}）`);
    R.check(await s.evaluate('document.querySelector("#stage").classList.contains("zoomed")'),
      '进入放大视图后画布可滚动（#stage.zoomed）');
    // 措辞必须诚实：预览是下采样栅格，不能说成"输出像素 1:1"
    const infoTxt = await s.evaluate('document.querySelector("#canvasInfo").textContent');
    R.check(infoTxt.includes('预览 100%') && infoTxt.includes('非输出像素'),
      `状态栏如实标注"按预览栅格、非输出像素"（${infoTxt.slice(-34)}）`);
    R.check(!infoTxt.includes('1:1 检查'), '不再声称是 1:1 检查（那会误导锐度判断）');
    if (SHOT) { await s.screenshot(SHOT); }
    await s.click(zBtn.x, zBtn.y);
    await s.sleep(400);
    const w200 = await s.evaluate('parseInt(document.querySelector("#stack").style.width, 10)');
    R.check(Math.abs(w200 - natW * 2) <= 3, `再点一次到 200%（${w200} vs ${natW * 2}）`);
    await s.click(zBtn.x, zBtn.y);
    await s.sleep(400);
    R.check(!(await s.evaluate('document.querySelector("#stage").classList.contains("zoomed")')),
      '第三次点回「适应窗口」');
    R.check((await s.evaluate('document.querySelector("#btnZoom").textContent')).trim() === '适应',
      '按钮文案随状态变化');

    // 集成：1:1 视图下对比功能仍要正常（擦除分割线依赖 #stack 的尺寸）
    await s.click(zBtn.x, zBtn.y);          // 回到 100%
    await s.sleep(400);
    const wipe = await s.centerOf('#cmpWipe');
    if (wipe) {
      await s.click(wipe.x, wipe.y);
      await s.sleep(300);
      const stackBox = await s.centerOf('#stack');
      const sp0 = await s.evaluate('CMP.split');
      await s.drag({ x: stackBox.left + 60, y: stackBox.top + 40 },
                   { x: stackBox.left + Math.round(stackBox.width * 0.7), y: stackBox.top + 40 }, 10);
      await s.sleep(400);
      const sp1 = await s.evaluate('CMP.split');
      R.check(sp1 !== sp0, `1:1 视图下擦除分割线仍可拖动（${Number(sp0).toFixed(2)} → ${Number(sp1).toFixed(2)}）`);
      R.check(await s.evaluate('!document.querySelector("#previewOrig").classList.contains("hidden")'),
        '1:1 视图下原图叠加仍正常显示');
      const st2 = await api('/api/state');
      R.check(st2.project.layers.length === 1 && st2.project.assets.length === 1,
        '在 1:1 视图里拖动没有误加图层/素材');
    }
    await s.evaluate('CMP.mode="off"; applyCompare();');
    await s.click(zBtn.x, zBtn.y);          // 200%
    await s.sleep(200);
    await s.click(zBtn.x, zBtn.y);          // 回适应
    await s.sleep(200);

    // ---- 快捷键
    const before = await s.evaluate('ST.project.layers[0].op_count');
    await s.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'z', code: 'KeyZ', modifiers: 2,
                                             windowsVirtualKeyCode: 90 });
    await s.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'z', code: 'KeyZ', modifiers: 2,
                                             windowsVirtualKeyCode: 90 });
    await s.sleep(900);
    const after = await s.evaluate('ST.project.layers[0].op_count');
    R.check(after < before, `Ctrl+Z 真的撤销了一步（${before} → ${after} 步）`);
    await s.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'y', code: 'KeyY', modifiers: 2,
                                             windowsVirtualKeyCode: 89 });
    await s.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'y', code: 'KeyY', modifiers: 2,
                                             windowsVirtualKeyCode: 89 });
    await s.sleep(900);
    R.check(await s.evaluate('ST.project.layers[0].op_count') > after, 'Ctrl+Y 真的重做了');

    // ---- 快捷键帮助
    await s.send('Input.dispatchKeyEvent', { type: 'keyDown', key: '?', code: 'Slash',
                                             modifiers: 8, windowsVirtualKeyCode: 191 });
    await s.send('Input.dispatchKeyEvent', { type: 'keyUp', key: '?', code: 'Slash',
                                             modifiers: 8, windowsVirtualKeyCode: 191 });
    await s.sleep(400);
    R.check(await s.evaluate('!!(document.querySelector("#helpDlg") && document.querySelector("#helpDlg").open)'),
      '按 ? 打开快捷键帮助');
    R.check((await s.evaluate('document.querySelector("#helpDlg").textContent')).includes('Ctrl'),
      '帮助里列出了真实存在的快捷键');
    const shot2 = arg('--shot2', '');
    if (shot2) { await s.screenshot(shot2); console.log(`  （截图已保存：${shot2}）`); }
    await s.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape',
                                             windowsVirtualKeyCode: 27 });
    await s.sleep(300);
    R.check(!(await s.evaluate('document.querySelector("#helpDlg").open')), 'Esc 关闭帮助');

    // ---- 面板宽度可拖
    const grip = await s.centerOf('#panelGrip');
    R.check(!!grip, '面板左边缘有可拖动的分隔条');
    const w0 = await s.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--panel-w').trim()");
    await s.drag({ x: grip.x, y: grip.y }, { x: grip.x - 120, y: grip.y }, 8);
    await s.sleep(400);
    const w1 = await s.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--panel-w').trim()");
    R.check(w0 !== w1, `拖动分隔条改变了面板宽度（${w0} → ${w1}）`);
    R.check(await s.evaluate("!!localStorage.getItem('cog_panel_w')"), '面板宽度记进了本地存储');

    // ---- 导出参数记忆
    await s.evaluate(`localStorage.setItem('cog_export_prefs',
      JSON.stringify({format:'png', quality:77, max_side:1500, dir:'D:\\\\成品'}))`);
    await s.evaluate('document.querySelector("#btnExport").click()');
    await s.sleep(400);
    const fx = await s.evaluate(`JSON.stringify({fmt: document.querySelector('#exFmt').value,
      q: document.querySelector('#exQ').value, max: document.querySelector('#exMax').value,
      dir: document.querySelector('#exPath').value})`);
    const f = JSON.parse(fx);
    R.check(f.fmt === 'png' && f.q === '77' && f.max === '1500' && f.dir.includes('成品'),
      `导出对话框回填上次参数（${fx}）`);
    await s.evaluate('document.querySelector("#exportDlg").close()');

    // ---- 长管线折叠 + 素材归属
    await s.evaluate('switchTab("layers")');
    await s.sleep(400);
    const rowsOf = `document.querySelectorAll('#tab-layers .ops .oprow:not(.fold)').length`;
    const nShown = await s.evaluate(rowsOf);
    R.check(nShown === 8, `10 步管线默认只显示 8 行（最新 6 + 最早 2），实际 ${nShown}`);
    const foldTxt = String(await s.evaluate(
      'document.querySelector("#tab-layers .oprow.fold")?.textContent || ""')).trim();
    R.check(foldTxt.length > 0, `出现折叠行（${foldTxt}）`);
    await s.evaluate('document.querySelector("#tab-layers .oprow.fold").click()');
    await s.sleep(400);
    const nAll = await s.evaluate(rowsOf);
    R.check(nAll === 10, `点折叠行后显示全部 10 步（实际 ${nAll}）`);
    R.check(await s.evaluate('!document.querySelector("#tab-layers .oprow.fold")'), '展开后折叠行消失');

    const assetTxt = await s.evaluate('document.querySelector("#tab-layers [data-asset]").textContent');
    R.check(assetTxt.includes('已用于'), `素材行标出用在哪些图层（${assetTxt.replace(/\\s+/g, ' ').trim().slice(0, 40)}）`);
    R.check(await s.evaluate(`document.querySelector('#tab-layers .head .chip')?.textContent.includes('素材')`),
      '图层头显示来源素材标签');
    const beforeSel = await s.evaluate('SEL.layer');
    await s.evaluate('document.querySelector("#tab-layers [data-asset][data-goto-layer]").click()');
    await s.sleep(300);
    R.check(await s.evaluate('SEL.layer') !== null && await s.evaluate('SEL.layer') === beforeSel,
      '点素材行会切到对应图层（本工程只有一个图层）');

    // ---- 输出像素查看（真正的 1:1：服务端全分辨率渲染 + 视口裁切）
    const pxBtn = await s.centerOf('#btnPixel');
    R.check(!!pxBtn, '顶栏有「输出像素」按钮');
    R.check(await s.evaluate('document.querySelector("#btnPixel").disabled === false'), '有工程时可用');
    await s.click(pxBtn.x, pxBtn.y);
    let px = null;
    for (let i = 0; i < 60; i++) {          // 首次全分辨率渲染可能要几秒
      await s.sleep(500);
      px = JSON.parse(await s.evaluate(`(() => {
        const im = document.querySelector('#preview');
        return JSON.stringify({src: im.getAttribute('src') || '', w: im.naturalWidth,
          h: im.naturalHeight, pixel: document.querySelector('#stage').classList.contains('pixel')});
      })()`));
      if (px.src.includes('/project/view') && px.w > 0) break;
    }
    R.check(px.src.includes('/project/view'), `切到输出像素查看（src 指向 /project/view）`);
    R.check(px.pixel, '#stage 进入 pixel 模式（隐藏对比叠加层）');
    const canvas = (await api('/api/state')).project.canvas;
    R.check(px.w <= canvas.w && px.h <= canvas.h && px.w > 100,
      `视口尺寸合理（${px.w}×${px.h}，画布 ${canvas.w}×${canvas.h}）`);
    const info2 = await s.evaluate('document.querySelector("#canvasInfo").textContent');
    R.check(info2.includes('输出像素'), `状态栏标出输出像素位置（${info2.slice(-42)}）`);
    R.check(!(await s.evaluate('document.querySelector("#cmpWrap").classList.contains("hidden") === false')),
      '输出像素模式下对比控件隐藏（避免与不同尺寸的基准硬叠）');
    if (SHOT) { await s.screenshot(SHOT.replace('.png', '-pixel.png')); }
    // 拖动平移：视口偏移应改变且重新拉图
    const box2 = await s.centerOf('#stack');
    const srcBefore = px.src;
    await s.drag({ x: box2.left + 200, y: box2.top + 150 },
                 { x: box2.left + 60, y: box2.top + 90 }, 10);
    await s.sleep(2500);
    const px2 = JSON.parse(await s.evaluate(`(() => {
      const im = document.querySelector('#preview');
      return JSON.stringify({src: im.getAttribute('src') || '', w: im.naturalWidth});
    })()`));
    R.check(px2.src !== srcBefore, '拖动后重新请求了新的视口区域（平移生效）');
    R.check(px2.w === px.w, '平移不改变视口宽度（仍是 1:1）');
    await s.click(pxBtn.x, pxBtn.y);
    await s.sleep(1200);
    R.check(await s.evaluate('!document.querySelector("#stage").classList.contains("pixel")'),
      '再点一次退出输出像素模式');
    R.check((await s.evaluate('document.querySelector("#preview").getAttribute("src")')).includes('preview'),
      '退出后恢复下采样预览');
  } finally {
    await s.close();
  }
  return R.finish();
}

main().then((c) => process.exit(c))
  .catch((e) => { console.error('检查脚本出错：', e.message); process.exit(2); });
