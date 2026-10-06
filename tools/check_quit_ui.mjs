// 驱动真实浏览器验证「退出」按钮是否有反应（使用共享的 tools/cdp.mjs）。
//
// 为什么需要它：用户报告"按退出没反应"。后端已被证明正常（HTTP 200、进程确实退出），
// 所以症状只可能出在前端交互路径上——那必须由浏览器来测，不能靠读代码断言。
//
// 关键手法：**安装一个 confirm 间谍**（window.confirm 返回 false，模拟浏览器"阻止此页面
// 创建更多对话框"后的行为）。若界面仍然有反应，说明它不再依赖原生弹窗；若没反应，
// 说明该按钮的成败系于原生 confirm —— 这正是本次故障的机制。
//
// 用法：node tools/check_quit_ui.mjs --base http://127.0.0.1:8793 [--shot <png>]
import { launch, reporter } from './cdp.mjs';

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = arg('--base', 'http://127.0.0.1:8793');
const SHOT = arg('--shot', '');

const R = reporter('退出按钮交互检查');

async function main() {
  const s = await launch({ cdpPort: Number(arg('--cdp', '9333')) });
  try {
    R.check(await s.goto(BASE + '/', '#btnQuit'), '页面已加载且存在「退出」按钮');
    await s.sleep(800);

    // 间谍：模拟"浏览器已阻止此页面创建更多对话框"（confirm 静默返回 false）
    await s.evaluate(`window.__confirmCalls = 0;
      window.confirm = () => { window.__confirmCalls++; return false; };`);

    const shutdownReqs = () => s.events.filter((e) =>
      e.method === 'Network.requestWillBeSent'
      && String(e.params?.request?.url || '').includes('/api/shutdown')).length;

    // ---- 场景 1：点「退出」后界面必须有可见反应（用户报的症状就在这里）
    await s.evaluate('document.querySelector("#btnQuit").click()');
    await s.sleep(1200);
    const r1 = JSON.parse(await s.evaluate(`(() => {
      const dlg = document.querySelector('#askDlg');
      return JSON.stringify({
        dlgOpen: !!(dlg && dlg.open),
        bye: /服务已退出|正在退出|正在关闭/.test(document.body.innerText),
      });
    })()`));
    R.check(r1.dlgOpen || r1.bye,
      `场景1（原生 confirm 被阻止）点击「退出」有可见反应：弹窗=${r1.dlgOpen} 告别文案=${r1.bye}`);
    if (SHOT) { await s.screenshot(SHOT); console.log(`  （截图已保存：${SHOT}）`); }

    // ---- 场景 2：必须不再依赖原生 confirm
    const calls = await s.evaluate('window.__confirmCalls');
    R.check(calls === 0, `场景2 界面不依赖原生 confirm（间谍被调用 ${calls} 次）`);

    // ---- 场景 3：确认后必须真的发出关闭请求、显示告别文案，并且服务确实停掉
    if (r1.dlgOpen) await s.evaluate('document.querySelector("#askOk").click()');
    await s.sleep(1800);
    const r3 = JSON.parse(await s.evaluate(`JSON.stringify({
      bye: /服务已退出|已退出/.test(document.body.innerText),
      text: document.body.innerText.slice(0, 100) })`));
    R.check(shutdownReqs() >= 1, `场景3 发出了 ${shutdownReqs()} 次 /api/shutdown 请求`);
    R.check(r3.bye, `场景3 页面进入告别状态：${r3.text.replace(/\s+/g, ' ').slice(0, 46)}`);
    await s.sleep(2500);
    let alive = true;
    try { alive = (await fetch(BASE + '/api/health')).ok; } catch { alive = false; }
    R.check(!alive, `场景3 服务确实已停止（health 可达=${alive}）`);
  } finally {
    await s.close();
  }
  return R.finish();
}

main().then((c) => process.exit(c))
  .catch((e) => { console.error('检查脚本出错：', e.message); process.exit(2); });
