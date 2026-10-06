// 生成 README / 文档用的界面截图。
//
// 两条纪律：
//   1. 只用合成示例图（tools/make_demo_photo.py 生成），绝不用真实照片；
//   2. 每张截图前把页面上出现的本机绝对路径改写为占位符（设置页会显示配置文件路径）。
//
// 用法：node tools/make_doc_shots.mjs --base http://127.0.0.1:8870 [--out docs/screenshots]
import { launch } from './cdp.mjs';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = arg('--base', 'http://127.0.0.1:8870');
const OUT = arg('--out', 'docs/screenshots');
const DEMO = arg('--demo', 'docs/demo/demo-photo.jpg');

async function api(path, body) {
  const r = await fetch(BASE + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return r.json();
}

// 把页面上任何绝对路径（含用户名）改写为占位符——截图要公开，不能带个人信息
const MASK = `(() => {
  const RX = /[A-Za-z]:\\\\[^\\s"'<>|]*/g;
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n = 0;
  while (walk.nextNode()) {
    const t = walk.currentNode;
    if (RX.test(t.nodeValue)) { t.nodeValue = t.nodeValue.replace(RX, 'C:\\\\…\\\\<你的目录>'); n++; }
  }
  return n;
})()`;

const shots = [];
async function shoot(s, name) {
  const masked = await s.evaluate(MASK);
  await s.sleep(120);
  const p = join(OUT, name);
  await s.screenshot(p);
  shots.push(`${name}${masked ? `（遮蔽了 ${masked} 处路径）` : ''}`);
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const opened = await api('/api/open', { paths: [DEMO], mode: 'new', name: 'demo-sunset' });
  if (!opened.project) throw new Error('无法准备示例工程：' + JSON.stringify(opened).slice(0, 200));
  const lid = opened.project.layers[0].id;
  await api('/api/ops', { ops: [
    { op: 'exposure', value: 0.15, layer: lid },
    { op: 'contrast', value: 10, layer: lid },
    { op: 'shadows', value: 12, layer: lid },
    { op: 'highlights', value: -14, layer: lid },
    { op: 'temperature', value: 8, layer: lid },
    { op: 'saturation', value: 6, layer: lid },
    { op: 'clarity', value: 8, layer: lid },
    { op: 'vignette', amount: 14, layer: lid },
    { op: 'sharpen', value: 22, layer: lid },
  ] });

  const s = await launch({ cdpPort: Number(arg('--cdp', '9350')) });
  try {
    await s.goto(BASE + '/', '#preview');
    await s.sleep(1500);

    // 1) 对话页
    await s.evaluate('switchTab("chat")');
    await s.sleep(500);
    await shoot(s, '01-chat.png');

    // 2) 图层页（9 步管线 + 素材归属 + 折叠）
    await s.evaluate('switchTab("layers")');
    await s.sleep(500);
    await shoot(s, '02-layers.png');

    // 3) 原图对比（擦除模式，左原图右成品）
    await s.evaluate('switchTab("chat")');
    await s.evaluate('CMP.mode="wipe"; CMP.split=0.42; applyCompare();');
    await s.sleep(600);
    await shoot(s, '03-compare.png');
    await s.evaluate('CMP.mode="off"; applyCompare();');
    await s.sleep(300);

    // 4) 输出像素查看（真 1:1）
    await s.evaluate('document.querySelector("#btnPixel").click()');
    for (let i = 0; i < 40; i++) {
      await s.sleep(400);
      const ok = await s.evaluate(
        'document.querySelector("#preview").naturalWidth > 0 && document.querySelector("#stage").classList.contains("pixel")');
      if (ok) break;
    }
    await s.sleep(600);
    await shoot(s, '04-output-pixels.png');
    await s.evaluate('document.querySelector("#btnPixel").click()');
    await s.sleep(800);

    // 5) 教学页（看图选择 + 评分维度）
    await s.evaluate('switchTab("teach")');
    await s.sleep(500);
    await shoot(s, '05-teaching.png');

    // 6) 设置页（模型列表 / 密钥状态 / 模型配置）
    await s.evaluate('switchTab("settings")');
    await s.sleep(500);
    await shoot(s, '06-settings.png');
  } finally {
    await s.close();
    await api('/api/project/close', {});
  }
  console.log('已生成截图：');
  for (const x of shots) console.log('  ' + x);
  return 0;
}

main().then((c) => process.exit(c))
  .catch((e) => { console.error('生成截图失败：', e.message); process.exit(1); });
