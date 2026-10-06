/* 机魂修图台前端：画布预览 + 自然语言对话 + 图层 + 微调滑杆 + 教学 + 设置。
   纯原生 JS，无构建步骤；所有状态变更都重新拉 /api/state，避免前后端两套真相。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let ST = null;          // /api/state 返回体
let SPECS = {};         // 算子手册：name -> spec
let MANUAL = null;
let SEL = { layer: null, opIndex: null };
let BUSY = 0;
// 对话区的本地临时消息（等待占位 / 失败回执），不落库；renderChat 会把它接在服务端记录之后
let LOCAL_MSGS = [];
// 已手动展开长管线的图层（默认折叠中间那段）
const OPEN_OPS = new Set();

// ---------------------------------------------------------------- 基础设施
function toast(msg, isErr = false, ms = 3800) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), ms);
}
let busyT0 = 0, busyTimer = null;
function busy(on, text = '处理中…') {
  const el = $('#busy'), tx = $('#busyText'), stage = $('#stage');
  BUSY = Math.max(0, BUSY + (on ? 1 : -1));
  if (on && BUSY === 1) { busyT0 = Date.now(); tx.dataset.base = text; }
  if (BUSY > 0) {
    // 长任务（抠图约 10s、AI 对话数十秒）光有转圈会让人以为卡死，所以显示已用时
    tx.textContent = tx.dataset.base || text;
    el.classList.add('show');
    if (stage) stage.setAttribute('aria-busy', 'true');
    if (!busyTimer) {
      busyTimer = setInterval(() => {
        const s = Math.floor((Date.now() - busyT0) / 1000);
        tx.textContent = s >= 3 ? `${tx.dataset.base || '处理中'}…已 ${s}s` : (tx.dataset.base || '处理中…');
      }, 1000);
    }
  } else {
    if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
    el.classList.remove('show');
    if (stage) stage.removeAttribute('aria-busy');
  }
}
async function api(path, body, opts = {}) {
  const init = { method: body === undefined ? 'GET' : 'POST', headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  // fetch 本身也会抛（服务正在退出、连接被重置）——必须捕获，
  // 否则调用方的“点完之后的反馈”会被异常跳过，表现为按钮点了没反应
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    return { ok: false, error: '无法连接本地服务（可能已退出或正在重启）：' + (e && e.message ? e.message : e) };
  }
  let data = null;
  try { data = await r.json(); } catch (e) { data = { ok: false, error: '服务返回异常：' + r.status }; }
  if (!r.ok && data && !data.error) data.error = 'HTTP ' + r.status;
  return data;
}

// 应用内确认框：**不要使用浏览器原生 confirm()**。
// 原生弹窗可能被浏览器静默阻止（"阻止此页面创建更多对话框"）而直接返回 false，
// 此时依赖它的按钮就会「点了没反应」——这正是曾经发生过的故障。
function askConfirm(title, text, okLabel = '确定') {
  return new Promise((resolve) => {
    const dlg = $('#askDlg');
    $('#askTitle').textContent = title;
    $('#askBody').textContent = text;
    const ok = $('#askOk'), cancel = $('#askCancel');
    ok.textContent = okLabel;
    const done = (v) => {
      ok.onclick = null; cancel.onclick = null; dlg.oncancel = null;
      try { dlg.close(); } catch (e) { /* 已关闭 */ }
      resolve(v);
    };
    ok.onclick = () => done(true);
    cancel.onclick = () => done(false);
    dlg.oncancel = (e) => { e.preventDefault(); done(false); };
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else { $('#askBody').textContent = text; resolve(false); }
  });
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function opSummary(op) {
  const spec = SPECS[op.op];
  const parts = [];
  for (const k of Object.keys(op)) {
    if (['op', 'layer', 'i', 'note', 'mask', 'mask_size'].includes(k)) continue;
    let v = op[k];
    if (typeof v === 'number') v = Math.round(v * 1000) / 1000;
    if (Array.isArray(v)) v = '[' + v.length + '项]';
    parts.push(k + '=' + v);
  }
  return (spec ? '' : '?') + parts.join(' ');
}

// ---------------------------------------------------------------- 渲染
function render() {
  const p = ST && ST.project;
  $('#projName').textContent = p ? `${p.name}　${p.canvas.w}×${p.canvas.h}　rev${p.rev}` : '未打开工程';
  $('#btnAdd').disabled = !p;
  $('#btnUndo').disabled = !p || !p.undo_depth;
  $('#btnRedo').disabled = !p || !p.redo_depth;
  $('#btnExport').disabled = !p;
  $('#btnZoom').disabled = !p;
  $('#btnPixel').disabled = !p;

  const stack = $('#stack'), img = $('#preview'), oimg = $('#previewOrig');
  if (p) {
    stack.classList.remove('hidden');
    $('#stageHint').classList.add('hidden');
    // 只在 revision 变化时换 src，避免每次 render 都重新下载预览造成闪烁
    if (String(stack.dataset.rev) !== String(p.rev)) {
      stack.dataset.rev = p.rev;
      if (PIXEL) {
        loadViewport();                     // 输出像素模式：改完立刻重渲当前视口
      } else {
        img.onload = fitPreview;
        img.src = p.preview_url;
        if (p.edited) {
          oimg.onload = fitPreview;
          oimg.src = p.original_url;      // 原图基准：只含几何校正，与成品同画幅
        }
      }
    }
    fitPreview();
    $('#canvasInfo').textContent = `画布 ${p.canvas.w}×${p.canvas.h}　底色 ${p.canvas.bg || '透明'}`
      + (p.preview_scale && p.preview_scale < 1
        ? `　预览按 ${Math.round(p.preview_scale * 100)}% 渲染（导出仍为全分辨率）` : '');
  } else {
    stack.classList.add('hidden');
    img.removeAttribute('src');
    oimg.removeAttribute('src');
    $('#stageHint').classList.remove('hidden');
    // 空状态：把「下一步做什么」和「回到上次的工程」直接摆出来，省掉一次找按钮
    const rec = (ST.recent || []).slice(0, 5);
    const hintBox = $('#hintRecent');
    if (hintBox) {
      hintBox.innerHTML = rec.length
        ? '<div class="hint" style="margin-top:12px">最近工程</div>' + rec.map(r =>
            `<div class="oprow" data-open="${esc(r.dir)}"><span class="nm">${esc(r.name)}</span>`
            + `<span class="vals">${esc(r.size || '')}　${esc(r.mtime || '')}</span></div>`).join('')
        : '';
    }
    $('#canvasInfo').textContent = '—';
  }
  applyCompare();
  renderChat(p);
  renderLayers(p);
  renderSettings();
  renderTuner(p);
}

function renderChat(p) {
  const box = $('#msgs');
  if (!p) { box.innerHTML = '<p class="hint">打开图片后即可在下方用自然语言下指令。</p>'; return; }
  // 本地临时消息：等待中的占位气泡与失败回执。不落库，刷新即消失（有意如此）。
  const items = [...(p.chat || []), ...LOCAL_MSGS]
    .filter(m => m.role === 'user' || m.role === 'assistant' || m.role === 'teacher');
  if (!items.length) {
    box.innerHTML = '<p class="hint">直接用自然语言描述想要的效果即可。<br>例：「整体提亮一点」「色温调冷」「把主体抠出来，背景换成浅灰」「用胶片暖调修一下」。</p>';
    return;
  }
  box.innerHTML = items.map(m => {
    // 等待中的占位气泡：让用户知道"已经发出去了、正在读图"，而不是对着空白反复点发送
    if (m.pending) {
      return `<div class="msg assistant pending">${esc(m.content)}<span class="dots">…</span></div>`;
    }
    if (m.failed) {
      return `<div class="msg assistant failed">${esc(m.content)}</div>`;
    }
    if (m.role === 'teacher') {
      return `<div class="msg assistant"><b style="color:var(--accent)">教学模式</b>\n${esc(m.content || '')}
        <div class="ops"><span class="hint">完整点评见「教学」标签页</span></div></div>`;
    }
    const cls = m.role === 'user' ? 'user' : 'assistant';
    if (m.role === 'user') return `<div class="msg user">${esc(m.content || '')}</div>`;
    const diag = m.diagnosis ? `<div class="diag">诊断：${esc(m.diagnosis)}</div>` : '';
    const intent = m.intent ? `<div class="intent">意图：${esc(m.intent)}</div>` : '';
    let opsHtml = '';
    if (m.ops && m.ops.length) {
      opsHtml = '<div class="ops">' + m.ops.map(o =>
        `<div class="op">▸ ${esc(o.op)} ${esc(opSummary(o))}</div>`).join('') + '</div>';
    }
    const chips = [];
    if (m.loaded && m.loaded.length) chips.push(`<span class="chip">参考 ${esc(m.loaded.join('、'))}</span>`);
    if (m.refine) {
      if (m.refine.error) chips.push(`<span class="chip warn">复核未完成</span>`);
      else if (m.refine.applied && m.refine.applied.length)
        chips.push(`<span class="chip refine">精修 ${m.refine.applied.length} 步</span>`);
      else if (m.refine.verdict) chips.push(`<span class="chip refine">复核：${esc(m.refine.verdict)}</span>`);
    }
    if (m.error) chips.push(`<span class="chip warn">未执行</span>`);
    const chipHtml = chips.length ? `<div class="chips">${chips.join('')}</div>` : '';
    const q = m.question ? `<div class="q">？${esc(m.question)}</div>` : '';
    return `<div class="msg assistant">${diag}${intent}${esc(m.content || '')}${opsHtml}${chipHtml}${q}</div>`;
  }).join('');
  box.parentElement.scrollTop = box.parentElement.scrollHeight;
}

let STYLES = [];        // 风格配方
function styleBar() {
  if (!STYLES.length) return '';
  const groups = {};
  STYLES.forEach(s => { (groups[s.group] = groups[s.group] || []).push(s); });
  const opts = Object.keys(groups).map(g =>
    `<optgroup label="${esc(g)}">` + groups[g].map(s =>
      `<option value="${esc(s.id)}" title="${esc(s.intent)}">${esc(s.name)}</option>`).join('') + '</optgroup>').join('');
  return `<div class="stylebar">
    <div class="hint">风格配方（意图 + 一串确定性算子，套用后每步仍可单独微调）</div>
    <select id="stylePick">${opts}</select>
    <div class="row2">
      <span class="hint">强度</span>
      <input type="range" id="styleStrength" min="0" max="150" step="5" value="80">
      <b id="styleStrengthVal" style="font-family:Consolas,monospace">80%</b>
      <button id="styleApply" class="primary">套用</button>
    </div>
    <div class="hint" id="styleIntent" style="margin-top:6px"></div>
  </div>`;
}

// 每个素材被哪些图层用到（不改接口，前端扫管线即可）：
//   · 初始导入的图层：其 source 文件就是素材文件
//   · 后来「加入图层」的：管线里有 add_layer 且 source === 'asset:<id>'
function assetUsage(p) {
  const map = {};
  for (const a of (p.assets || [])) {
    const used = (p.layers || []).filter(l =>
      l.source === a.file ||
      (l.ops || []).some(o => o.op === 'add_layer' && String(o.source || '') === 'asset:' + a.id)
    ).map(l => l.id);
    map[a.id] = { used, file: a.file, name: a.name };
  }
  return map;
}

function assetStrip(p) {
  if (!p.assets || !p.assets.length) return '';
  const raws = p.assets.filter(a => a.source_kind === 'raw');
  const usage = assetUsage(p);
  return `<div class="stylebar">
    <div class="hint">素材库（点「加入图层」放进当前工程；多图合成就从这里开始）</div>
    ${p.assets.map(a => {
      const dec = a.decoded === 'rawpy' ? 'RAW 解码' : (a.decoded === 'embedded-jpeg' ? 'RAW 内嵌预览' : '');
      const u = usage[a.id] || { used: [] };
      const usedTxt = u.used.length
        ? ` · <b style="color:var(--ok)">已用于 ${esc(u.used.join('、'))}</b>` : '';
      return `<div class="oprow" data-asset="${esc(a.id)}"${u.used.length ? ` data-goto-layer="${esc(u.used[0])}" title="点击跳到用到它的图层"` : ''}>
        <span class="nm">${esc(a.id)}</span>
        <span class="vals">${esc(a.name)} · ${a.size[0]}×${a.size[1]}` +
        (dec ? ` · <b style="color:var(--accent2)">${dec}</b>` : '') + usedTxt + `</span>
        ${u.used.length
          ? '<button class="ghost" data-addasset="1" style="font-size:11px;padding:2px 7px" title="再加一份到新图层">再加一份</button>'
          : '<button class="ghost" data-addasset="1" style="font-size:11px;padding:2px 7px">加入图层</button>'}
      </div>`;
    }).join('')}
    ${raws.length ? `<div class="hint">RAW 取的是文件内嵌的相机渲染图：分辨率完整，但没有 RAW 高光宽容度（安装 rawpy 后可改走真解码）。</div>` : ''}
  </div>`;
}

function renderLayers(p) {
  const box = $('#tab-layers');
  if (!p) { box.innerHTML = '<p class="hint">还没有工程。</p>'; return; }
  const blends = ['normal', 'multiply', 'screen', 'overlay', 'darken', 'lighten', 'add', 'soft_light', 'difference'];
  const list = [...p.layers].reverse();   // 上层显示在上
  const usage = assetUsage(p);
  box.innerHTML = styleBar() + assetStrip(p) + list.map(l => {
    const isActive = l.id === (SEL.layer || p.active);
    // 长管线折叠：十几步时要滚很久才找得到开头的调色。默认「最新 6 步 + 最早 2 步」，
    // 中间折成一行；被选中的步骤（微调目标）所在图层强制展开，避免滑杆失去落点。
    const total = l.ops.length;
    const tuneHere = SEL.layer === l.id && SEL.opIndex != null;
    const expanded = OPEN_OPS.has(l.id) || tuneHere;
    let order = [];
    if (!total) order = [];
    else if (total <= 8 || expanded) order = Array.from({ length: total }, (_, i) => total - 1 - i);
    else order = [total - 1, total - 2, total - 3, total - 4, total - 5, total - 6, -1, 1, 0];
    const opsHtml = total ? `<div class="ops">` + order.map(idx => {
      if (idx === -1) {
        return `<div class="oprow fold" data-expand="${l.id}" title="点开查看全部 ${total} 步">`
          + `▸ 中间折叠了 ${total - 8} 步（点开）</div>`;
      }
      const oo = l.ops[idx];
      const sel = tuneHere && SEL.opIndex === idx ? ' sel' : '';
      return `<div class="oprow${sel}" data-layer="${l.id}" data-idx="${idx}">
        <span class="nm">${esc(oo.op)}</span><span class="vals">${esc(opSummary(oo))}</span>
        <span class="x" data-del="1" title="删掉这一步">✕</span></div>`;
    }).join('') + '</div>' : '<p class="hint" style="margin:6px 0 0">还没有应用任何操作</p>';
    const fromAsset = (p.assets || []).find(a => (usage[a.id] || {}).used.includes(l.id));
    const assetChip = fromAsset ? `<span class="chip" title="这个图层来自素材 ${esc(fromAsset.id)}">素材 ${esc(fromAsset.id)}</span>` : '';
    return `<div class="layer${isActive ? ' active' : ''}" data-layer="${l.id}">
      <div class="head">
        <img class="thumb" src="/project/source/${l.id}?thumb=64" alt="">
        <span class="id">${l.id}</span>
        <span class="name" data-act="active" title="点击切换为当前活动图层">${esc(l.name)}${l.has_mask ? ' ✂' : ''}</span>${assetChip}
        <button class="ghost" data-act="vis" title="显示/隐藏">${l.visible ? '👁' : '🚫'}</button>
        <button class="ghost" data-act="up" title="上移一层">▲</button>
        <button class="ghost" data-act="down" title="下移一层">▼</button>
        <button class="ghost" data-act="del" title="删除图层">✕</button>
      </div>
      <div class="body">
        <div class="grid2">
          <div><label>不透明度 ${l.opacity}</label>
            <input type="range" min="0" max="1" step="0.01" value="${l.opacity}" data-act="opacity"></div>
          <div><label>混合模式</label>
            <select data-act="blend">${blends.map(b => `<option ${b === l.blend ? 'selected' : ''}>${b}</option>`).join('')}</select></div>
        </div>
        <div class="grid3" style="margin-top:6px">
          <div><label>位置 X</label><input type="number" value="${Math.round(l.x)}" data-act="x"></div>
          <div><label>位置 Y</label><input type="number" value="${Math.round(l.y)}" data-act="y"></div>
          <div><label>缩放</label><input type="number" step="0.01" value="${l.scale}" data-act="scale"></div>
        </div>
        <div class="grid3" style="margin-top:6px">
          <button data-act="fit" title="把图层缩放成刚好装进画布（不改画布）">图层适配画布</button>
          <button data-act="fill" title="把图层缩放成铺满画布（可能裁掉边缘）">图层铺满画布</button>
          <button data-act="center">居中</button>
        </div>
        <div class="grid2" style="margin-top:6px">
          <button data-act="canvasfit" title="把画布缩到刚好包住内容 —— 裁切之后用它可去掉导出时的白边（等于接管画布，之后裁切不再自动适配）">画布适配内容</button>
          <button data-act="canvasratio" title="把画布改成 1:1（内容不动，仅扩/裁画幅）">画布改 1:1</button>
        </div>
        <div class="grid3" style="margin-top:6px">
          <button data-act="fliph">水平翻转</button>
          <button data-act="flipv">垂直翻转</button>
          <button data-act="rot90">旋转90°</button>
        </div>
        <div class="grid2" style="margin-top:6px">
          <button data-act="cut" class="primary" title="在本机离线抠图（BiRefNet）">一键抠图</button>
          <button data-act="clearmask">清除蒙版</button>
        </div>
        <div class="hint" style="margin-top:6px">输出 ${l.out_size[0]}×${l.out_size[1]}　源图 ${l.src_size.join('×')}　共 ${l.op_count} 步</div>
        ${opsHtml}
      </div>
    </div>`;
  }).join('');
  $('#layerInfo').textContent = p.layers.length ? `图层 ${p.layers.length} 个　当前 ${SEL.layer || p.active}` : '';
  const pick = $('#stylePick');
  if (pick) {
    const cur = STYLES.find(s => s.id === pick.value) || STYLES[0];
    $('#styleIntent').innerHTML = cur
      ? `意图：${esc(cur.intent)}${cur.caution ? `<br><span style="color:var(--warn)">注意：${esc(cur.caution)}</span>` : ''}`
      : '';
  }
}

function renderTuner(p) {
  const wrap = $('#tunerToggleWrap');
  const tuner = $('#tuner');
  if (!p || SEL.layer == null || SEL.opIndex == null) {
    wrap.classList.add('hidden'); tuner.classList.remove('show'); return;
  }
  const layer = p.layers.find(l => l.id === SEL.layer);
  const op = layer && layer.ops[SEL.opIndex];
  if (!op) { wrap.classList.add('hidden'); tuner.classList.remove('show'); return; }
  wrap.classList.remove('hidden');
  const spec = SPECS[op.op];
  const numeric = (spec ? spec.params : []).filter(pp =>
    (pp.kind === 'number' || pp.kind === 'int') && op[pp.name] !== undefined);
  $('#tunerTitle').textContent = `微调：图层 ${SEL.layer} 第 ${SEL.opIndex} 步 ${op.op}` +
    (numeric.length ? '（拖动即时生效）' : '（该步骤没有可调数值）');
  $('#tunerRows').innerHTML = numeric.map(pp => {
    const val = op[pp.name];
    const min = pp.min == null ? 0 : pp.min, max = pp.max == null ? 100 : pp.max;
    const step = pp.kind === 'int' ? 1 : (max - min) / 200;
    return `<div class="tunerrow"><span>${esc(pp.name)}</span>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${val}"
        data-op-key="${esc(pp.name)}" data-layer="${SEL.layer}" data-idx="${SEL.opIndex}">
      <b>${Math.round(val * 1000) / 1000}</b></div>`;
  }).join('');
  if (!numeric.length) tuner.classList.remove('show');
}

function renderSettings() {
  const s = ST && ST.settings;
  if (!s) return;
  renderTeachBar();
  const box = $('#tab-settings');
  const cur = s.active_model;
  const models = s.models.map(m => {
    const hasKey = (s.models_with_key || []).includes(m.id) || (s.has_env_key || {})[m.id];
    return `<div class="mdl${m.id === cur ? ' cur' : ''}" data-id="${esc(m.id)}">
      <div class="top">
        <span class="nm">${esc(m.name)}</span>
        ${m.vision ? '<span class="tag vision">可读图</span>' : '<span class="tag no">不读图</span>'}
        <span class="tag ${hasKey ? 'ok' : 'no'}">${hasKey ? '有密钥' : '缺密钥'}</span>
        ${m.id === cur ? '<span class="tag ok">当前</span>' : ''}
      </div>
      <div class="hint" style="margin-top:4px">${esc(m.model || '（未填模型名）')} · ${esc(m.base_url || '（未填 base_url）')}</div>
      <div style="display:flex;gap:6px;margin-top:7px;flex-wrap:wrap">
        <button data-mdl="use" data-id="${esc(m.id)}">设为当前</button>
        <button data-mdl="teach" data-id="${esc(m.id)}" title="教学模式用的模型">设为教学模型</button>
        <button data-mdl="edit" data-id="${esc(m.id)}">编辑</button>
        <button data-mdl="test" data-id="${esc(m.id)}">测试连通</button>
      </div>
    </div>`;
  }).join('');

  box.innerHTML = `
  <div class="sect"><h3>模型（默认 DeepSeek）</h3>
    ${models}
    <div class="hint">密钥只保存在本机 config/settings.json 或对应环境变量里，不会进入对话记录与工程文件。</div>
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <button data-mdl="new">＋ 新增模型</button>
      <button data-mdl="reset">恢复预设清单</button>
    </div>
    <div id="mdlForm"></div>
  </div>
  <div class="sect"><h3>教学模式</h3>
    <label>教学使用的模型（必须支持读图）</label>
    <select id="teachModel">${['<option value="">跟随当前模型</option>']
      .concat(s.models.filter(m => m.vision).map(m =>
        `<option value="${esc(m.id)}" ${s.teaching_model === m.id ? 'selected' : ''}>${esc(m.name)}</option>`)).join('')}</select>
    <div class="hint">当前教学模型：${esc((s.models.find(m => m.id === (s.teaching_model || s.active_model)) || {}).name || '—')}</div>
  </div>
  <div class="sect"><h3>抠图</h3>
    <label>引擎</label>
    <select id="mgEngine">
      ${[['auto', '自动（优先 BiRefNet，失败退 GrabCut）'], ['birefnet', '只用 BiRefNet'], ['grabcut', '只用 GrabCut']]
        .map(([v, t]) => `<option value="${v}" ${s.matting.engine === v ? 'selected' : ''}>${t}</option>`).join('')}
    </select>
    <label>权重目录（BiRefNet / RMBG-2.0）</label>
    <input id="mgDir" value="${esc(s.matting.model_dir || '')}">
    <div class="grid2">
      <div><label>计算设备</label>
        <select id="mgDev">${['auto', 'cuda', 'cpu'].map(v =>
          `<option ${s.matting.device === v ? 'selected' : ''}>${v}</option>`).join('')}</select></div>
      <div><label>推理分辨率</label><input id="mgRes" type="number" value="${s.matting.resolution}"></div>
    </div>
    <div id="mgStatus" class="hint" style="margin-top:6px"></div>
    <div style="display:flex;gap:6px;margin-top:6px"><button id="mgSave">保存抠图设置</button>
      <button id="mgRel">释放显存</button><button id="mgCheck">检测状态</button></div>
  </div>
  <div class="sect"><h3>预览与导出</h3>
    <div class="grid2">
      <div><label>预览最长边</label><input id="pvSide" type="number" value="${s.preview.max_side}"></div>
      <div><label>预览质量</label><input id="pvQ" type="number" value="${s.preview.jpeg_quality}"></div>
    </div>
    <button id="pvSave" style="margin-top:6px">保存预览设置</button>
  </div>
  <div class="sect"><h3>审美技能包</h3>
    <div id="skillStatus" class="hint">读取中…</div>
    <div id="skillList"></div>
    <div style="display:flex;gap:6px;margin-top:8px">
      <button id="skReload">重新加载技能包与配方</button>
    </div>
    <div class="hint" style="margin-top:6px">核心准则每轮常驻；大部头参考由模型按需请求加载（点标题可预览正文，也可自己编辑 skills/ 目录下的 md 文件）。</div>
  </div>
  <div class="sect"><h3>对话与自动精修</h3>
    <label class="switch" style="margin-top:4px"><input type="checkbox" id="cfRefine" ${s.chat.auto_refine ? 'checked' : ''}>
      <span>应用后自动精修（把成图再交给模型复核一轮，可回退）</span></label>
    <div class="grid2">
      <div><label>每轮最多加载参考</label><input id="cfRefs" type="number" min="0" max="4" value="${s.skills.max_refs_per_turn}"></div>
      <div><label>复核最多改几步</label><input id="cfRefineMax" type="number" min="0" max="6" value="${s.chat.refine_max_ops}"></div>
    </div>
    <button id="cfSave" style="margin-top:6px">保存对话设置</button>
  </div>
  <div class="sect"><h3>网络</h3>
    <label class="switch"><input type="checkbox" id="nwUse" ${s.network.use_proxy ? 'checked' : ''}>
      <span>通过代理访问模型接口</span></label>
    <label>代理地址</label>
    <input id="nwProxy" value="${esc(s.network.proxy || '')}" placeholder="http://127.0.0.1:7897">
    <div style="display:flex;gap:6px;margin-top:6px">
      <button id="nwSave">保存网络设置</button>
      <button id="nwProbe">检测网络</button>
    </div>
    <div id="nwResult" class="hint" style="margin-top:6px">直连通常够用；若模型接口超时或被墙，再启用代理。</div>
  </div>
  <div class="sect"><h3>RAW（CR3 / CR2 / NEF / ARW / DNG …）</h3>
    <label>解码方式</label>
    <select id="rawMode">
      ${[['auto', '自动（rawpy → WIC → 内嵌预览，依次回退）'],
         ['rawpy', '只用 rawpy（16bit，可救高光）'],
         ['wic', '只用 Windows RAW 影像扩展（8bit，系统解码）'],
         ['preview', '只用内嵌预览（最快，零依赖）']]
        .map(([v, t]) => `<option value="${v}" ${s.raw.decode === v ? 'selected' : ''}>${t}</option>`).join('')}
    </select>
    <div id="rawStatus" class="hint" style="margin-top:6px">读取中…</div>
    <button id="rawSave" style="margin-top:6px">保存 RAW 设置</button>
  </div>
  <div class="sect"><h3>最近工程</h3>
    ${(ST.recent || []).length ? ST.recent.map(r =>
      `<div class="oprow" data-open="${esc(r.dir)}"><span class="nm">${esc(r.name)}</span>
        <span class="vals">${esc(r.size)} · ${esc(r.mtime)}</span></div>`).join('')
      : '<p class="hint">还没有历史工程。</p>'}
  </div>
  <div class="sect"><h3>关于</h3>
    <p class="hint">机魂修图台 v${ST.version || ''}<br>
    AI 只输出结构化编辑指令，像素改动全部由本机确定性算子完成 —— 不重绘，因此细节不漂移；每一步都能撤销与微调。<br>
    审美来自三处：常驻的领域准则、可套用的确定性风格配方、以及逼模型"先诊断后动手"的提示词纪律。<br>
    配置文件：${esc(s.settings_file || '')}</p>
  </div>`;
  refreshMattingStatus();
  refreshSkillsPanel();
  refreshRawStatus();
}

async function refreshRawStatus() {
  const el = $('#rawStatus');
  if (!el) return;
  const r = await api('/api/raw/status');
  if (!r || !r.ok) { el.textContent = 'RAW 状态未知'; return; }
  const rows = (r.methods || []).map(m =>
    `<tr><td>${m.available ? '<span class="ok2">✔</span>' : '<span class="bad2">✘</span>'} ${esc(m.label)}</td>
      <td>${esc(m.quality)}</td><td>${esc(m.cost)}</td></tr>`).join('');
  el.innerHTML = `本机将使用：<b style="color:var(--accent)">${esc(r.auto_will_use)}</b>`
    + `（模式 ${esc(r.decode_mode)}）　支持 ${r.raw_extensions.length} 种 RAW 格式`
    + `<table class="probe"><tr><th>路径</th><th>画质</th><th>代价</th></tr>${rows}</table>`
    + `<div class="hint">${esc(r.note)}</div>`
    + (r.methods || []).filter(m => !m.available).map(m => `<div class="hint">· ${esc(m.label)}：${esc(m.note)}</div>`).join('');
}

async function refreshSkillsPanel() {
  const el = $('#skillStatus');
  if (!el) return;
  const r = await api('/api/skills');
  if (!r || !r.ok) { el.textContent = '技能包状态未知'; return; }
  el.innerHTML = `目录：${esc(r.dir)}<br>共 <b>${r.packs.length}</b> 个技能包、<b>${r.reference_count}</b> 份参考，`
    + `已启用 <b>${r.enabled_count}</b> 个包。`;
  $('#skillList').innerHTML = r.packs.map(p => `
    <div class="mdl" style="margin-top:8px">
      <div class="top"><span class="nm">${esc(p.name)}</span>
        <span class="tag ${p.enabled ? 'ok' : 'no'}">${p.enabled ? '已启用' : '未启用'}</span>
        <span class="tag">${p.references.length} 份参考</span></div>
      <div class="hint" style="margin-top:4px">${esc(p.description)}</div>
      <div class="filelist" style="margin-top:6px">${(r.refs || []).filter(x => x.pack === p.id).map(x => `
        <div data-ref="${esc(x.id)}"><span class="nm">📄 ${esc(x.title)}</span>
          <span class="meta">${esc(x.summary || '')}</span></div>`).join('')}</div>
    </div>`).join('');
}

async function refreshMattingStatus() {
  const el = $('#mgStatus');
  if (!el) return;
  const r = await api('/api/matting/status');
  if (!r || !r.ok) { el.textContent = '抠图状态未知'; return; }
  const s = r.status;
  el.innerHTML = `权重：${s.weights_found ? '<span style="color:var(--ok)">已就位</span>' : '<span style="color:var(--err)">未找到</span>'}`
    + `　模型：${s.loaded ? '已加载' : '未加载'}　设备：${esc(s.device)}`
    + (s.load_seconds ? `　装载耗时 ${s.load_seconds}s` : '')
    + (s.error ? `<br><span style="color:var(--warn)">上次报错：${esc(s.error)}</span>` : '');
}

// ---------------------------------------------------------------- 交互

// 标签切换
function switchTab(name) {
  $$('.tabs button').forEach(x => x.classList.toggle('active', x.dataset.tab === name));
  ['chat', 'layers', 'teach', 'settings'].forEach(t =>
    $('#tab-' + t).classList.toggle('hidden', t !== name));
  $('#chatbar').style.display = name === 'chat' ? '' : 'none';
  if (name === 'settings') refreshMattingStatus();
  if (location.hash.slice(1) !== name) history.replaceState(null, '', '#' + name);
}
$$('.tabs button').forEach(b => b.onclick = () => switchTab(b.dataset.tab));

async function doUndo() {
  if (BUSY) return toast('上一步还没完成，稍等一下', true);
  busy(true, '撤销中…');
  const r = await api('/api/undo', {});
  busy(false);
  if (!r.ok) return toast(r.error, true);
  if (!r.changed) return toast('没有可撤销的步骤');
  applyState(r.project);
}
async function doRedo() {
  if (BUSY) return toast('上一步还没完成，稍等一下', true);
  busy(true, '重做中…');
  const r = await api('/api/redo', {});
  busy(false);
  if (!r.ok) return toast(r.error, true);
  if (!r.changed) return toast('没有可重做的步骤');
  applyState(r.project);
}

function applyState(project) {
  ST.project = project;
  if (project) {
    if (!SEL.layer || !project.layers.some(l => l.id === SEL.layer)) {
      SEL.layer = project.active || (project.layers[0] && project.layers[0].id);
      SEL.opIndex = null;
    }
    const l = project.layers.find(x => x.id === SEL.layer);
    if (l && SEL.opIndex != null && SEL.opIndex >= l.ops.length) SEL.opIndex = l.ops.length - 1;
  }
  render();
}

async function sendChat() {
  const box = $('#chatInput');
  const text = box.value.trim();
  if (!text) return;
  if (!ST.project) return toast('请先打开图片', true);
  if (BUSY) return toast('上一步还没完成，稍等一下', true);
  box.value = '';
  // 立刻插一条"已发出"的占位气泡：等待几十秒时用户才知道指令没丢
  LOCAL_MSGS = [{ role: 'user', content: text },
                { role: 'assistant', pending: true, content: '模型正在读图分析，通常几秒到几十秒' }];
  renderChat(ST.project);
  const model = $('#modelPick').value;
  if (model && model !== ST.settings.active_model) await api('/api/settings', { active_model: model });
  busy(true, '模型思考中…');
  const r = await api('/api/chat', { message: text, refine: $('#refineToggle').checked });
  busy(false);
  if (!r.ok) {
    // 失败时把话还给用户：改一句就能重发，而不是重新打一遍
    LOCAL_MSGS = [{ role: 'user', content: text },
                  { role: 'assistant', failed: true, content: '这次没有完成：' + r.error }];
    box.value = text;
    renderChat(ST.project);
    toast(r.error, true, 8000);
    return;
  }
  LOCAL_MSGS = [];                       // 服务端已记录这一轮，撤掉占位
  if (r.reply.error) toast(r.reply.error, true, 9000);
  else {
    const bits = [];
    if (r.reply.loaded_titles && r.reply.loaded_titles.length) bits.push('加载了 ' + r.reply.loaded_titles.join('、'));
    if (r.reply.applied && r.reply.applied.length) bits.push('应用 ' + r.reply.applied.length + ' 步');
    if (r.reply.refine && r.reply.refine.applied && r.reply.refine.applied.length)
      bits.push('精修 ' + r.reply.refine.applied.length + ' 步');
    if (bits.length) toast(bits.join(' · '));
  }
  applyState(r.project);
  await refreshSettingsQuiet();
}

async function refreshSettingsQuiet() {
  const r = await api('/api/settings');
  if (r && r.ok) { ST.settings = r.settings; if (r.presets) ST.presets = r.presets; fillModelPick(); }
}

function fillModelPick() {
  const s = ST.settings;
  const sel = $('#modelPick');
  sel.innerHTML = s.models.map(m => {
    const mark = m.vision && !/读图/.test(m.name) ? '（可读图）' : '';
    return `<option value="${esc(m.id)}" ${m.id === s.active_model ? 'selected' : ''}>${esc(m.name)}${mark}</option>`;
  }).join('');
}

// 预览自适应：画布比窗口小时放大到看得清（最多 3 倍），大时缩到放得下
// ZOOM='fit' 适应窗口；1 / 2 表示 100% / 200% 的 1:1 像素视图（判断锐度必须用 1:1）
let ZOOM = 'fit';
function fitPreview() {
  const img = $('#preview'), stage = $('#stage'), stack = $('#stack');
  if (!img.naturalWidth || stack.classList.contains('hidden')) return;
  const zoomed = ZOOM !== 'fit';
  stage.classList.toggle('zoomed', zoomed);
  if (zoomed) {
    stack.style.width = Math.round(img.naturalWidth * ZOOM) + 'px';
    stack.style.height = Math.round(img.naturalHeight * ZOOM) + 'px';
    return;
  }
  const sw = stage.clientWidth - 56, sh = stage.clientHeight - 56;
  const s = Math.min(sw / img.naturalWidth, sh / img.naturalHeight, 3);
  stack.style.width = Math.max(40, Math.round(img.naturalWidth * s)) + 'px';
  stack.style.height = Math.max(40, Math.round(img.naturalHeight * s)) + 'px';
}
function cycleZoom() {
  ZOOM = ZOOM === 'fit' ? 1 : (ZOOM === 1 ? 2 : 'fit');
  const b = $('#btnZoom');
  if (b) b.textContent = ZOOM === 'fit' ? '适应' : `预览 ${ZOOM * 100}%`;
  if (b) b.classList.toggle('on', ZOOM !== 'fit');
  fitPreview();
  const info = $('#canvasInfo');
  if (info && ST.project) {
    // 注意措辞：预览是按显示尺寸渲染的下采样栅格（例如 6000px 的图预览只有 1400px），
    // 所以这里的 100% 是"预览像素 1:1"，观感等于把成片缩小到该尺寸看 —— 不是输出像素 1:1。
    // 说成"1:1 检查"会让人以为能判断输出锐度，那是错的。
    const t = ZOOM === 'fit' ? '' : `　预览 ${ZOOM * 100}%（按预览栅格，非输出像素）`;
    info.textContent = info.textContent.replace(/　预览 \d+%（按预览栅格.*$/, '') + t;
  }
  toast(ZOOM === 'fit' ? '预览：适应窗口'
    : `预览 ${ZOOM * 100}%：按预览像素显示（≈成片缩到 ${Math.round((ST.project?.preview_scale || 1) * ZOOM * 100)}% 观看的效果）`);
}
window.addEventListener('resize', fitPreview);

// ---------------------------------------------------------------- 输出像素查看
// 为什么单独做：预览是按显示尺寸渲染的下采样栅格（6000px 的照片预览只有约 1400px），
// 所以"预览 100%"只是预览像素的 1:1，看不到输出像素级的锐化。这里改为向服务端要
// **全分辨率渲染后裁出的视口**（服务端按 rev 缓存整幅渲染，平移只做裁剪+编码）。
let PIXEL = false;
const VIEW = { x: 0, y: 0, seq: 0, timer: null };

function pixelOn() { return PIXEL && !!(ST && ST.project); }

function stageInner() {
  const st = $('#stage');
  return { w: Math.max(160, st.clientWidth - 36), h: Math.max(120, st.clientHeight - 36) };
}

function loadViewport(delay = 0) {
  clearTimeout(VIEW.timer);
  VIEW.timer = setTimeout(() => {
    const p = ST && ST.project;
    if (!pixelOn()) return;
    const W = p.canvas.w, H = p.canvas.h;
    const box = stageInner();
    const w = Math.max(16, Math.min(box.w, W));
    const h = Math.max(16, Math.min(box.h, H));
    VIEW.x = Math.max(0, Math.min(VIEW.x, W - w));
    VIEW.y = Math.max(0, Math.min(VIEW.y, H - h));
    const seq = ++VIEW.seq;
    const img = $('#preview'), stack = $('#stack');
    busy(true, '渲染输出像素…（首次约数秒）');
    img.onload = () => {
      if (seq !== VIEW.seq) return;
      busy(false);
      stack.style.width = img.naturalWidth + 'px';
      stack.style.height = img.naturalHeight + 'px';
      const info = $('#canvasInfo');
      if (info) {
        info.textContent = info.textContent.replace(/　输出像素.*$/, '')
          + `　输出像素 ${VIEW.x},${VIEW.y} 起 ${img.naturalWidth}×${img.naturalHeight}（1:1，拖动可平移）`;
      }
    };
    img.onerror = () => { if (seq === VIEW.seq) { busy(false); toast('输出像素渲染失败', true); } };
    img.src = `/project/view?x=${VIEW.x}&y=${VIEW.y}&w=${w}&h=${h}&t=${p.rev}`;
  }, delay);
}

function togglePixel() {
  if (!ST.project) return;
  PIXEL = !PIXEL;
  const b = $('#btnPixel'), stage = $('#stage'), img = $('#preview');
  if (b) { b.classList.toggle('on', PIXEL); b.textContent = PIXEL ? '输出像素 ✕' : '输出像素'; }
  stage.classList.toggle('pixel', PIXEL);
  if (PIXEL) {
    ZOOM = 'fit';                                   // 两种查看方式互斥
    const zb = $('#btnZoom');
    if (zb) { zb.textContent = '适应'; zb.classList.remove('on'); }
    VIEW.x = 0; VIEW.y = 0;
    applyCompare();
    loadViewport();
  } else {
    img.onload = fitPreview;
    img.src = ST.project.preview_url;               // 恢复下采样预览
    loadViewportCancel();
    const info = $('#canvasInfo');
    if (info) info.textContent = info.textContent.replace(/　输出像素.*$/, '');
    render();                                       // 重画状态栏与对比控件
  }
}
function loadViewportCancel() { clearTimeout(VIEW.timer); VIEW.seq++; busy(false); }

// 输出像素模式下拖动平移（1:1，所以拖动多少 CSS 像素就是多少输出像素）
(() => {
  const stage = $('#stage');
  let panning = false, last = null;
  stage.addEventListener('pointerdown', (e) => {
    if (!pixelOn() || e.button !== 0) return;
    if (e.target.closest('button, #cmpWrap, .stagebadge')) return;
    e.preventDefault();
    panning = true; last = { x: e.clientX, y: e.clientY };
    try { stage.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
  });
  stage.addEventListener('pointermove', (e) => {
    if (!panning || !pixelOn()) return;
    VIEW.x -= (e.clientX - last.x);
    VIEW.y -= (e.clientY - last.y);
    last = { x: e.clientX, y: e.clientY };
    loadViewport(90);
  });
  const up = () => { panning = false; };
  stage.addEventListener('pointerup', up);
  window.addEventListener('pointerup', up);
})();

// ---------------------------------------------------------------- 原图对比
// 两种模式：按住看原图（瞬时）、左右擦除（可拖动分割线）。
// 原图是"只含几何校正"的基准，因此与成品同画幅、可逐像素对齐地比较（见 document.render_original）。
const CMP = { mode: 'off', split: 0.5, hold: false };

function cmpAvailable() {
  // 输出像素查看模式下不做原图对比：那个模式显示的是服务端裁出的视口，
  // 与下采样预览的叠加基准不同尺寸，硬叠会对不齐（将来看需要可给视口也做一份基准）。
  return !!(ST && ST.project && ST.project.edited) && !PIXEL;
}

function applyCompare() {
  const stack = $('#stack'), orig = $('#previewOrig'), line = $('#wipeLine');
  const bO = $('#badgeOrig'), bN = $('#badgeNew'), wrap = $('#cmpWrap');
  if (!stack) return;
  const ok = cmpAvailable();
  wrap.classList.toggle('hidden', !ok);
  if (!ok) { CMP.mode = 'off'; CMP.hold = false; }
  const wipe = $('#cmpWipe'), off = $('#cmpOff');
  if (wipe) wipe.classList.toggle('on', CMP.mode === 'wipe');
  if (off) off.classList.toggle('on', CMP.mode === 'off');

  // 按住优先：无论当前处于哪种模式，按下即看原图、松开即回成品
  let showOrig = CMP.hold;
  if (!showOrig && CMP.mode === 'wipe') {
    showOrig = true;
    orig.style.clipPath = `inset(0 ${(1 - CMP.split) * 100}% 0 0)`;
  } else {
    orig.style.clipPath = 'none';
  }
  orig.classList.toggle('hidden', !showOrig);
  line.classList.toggle('hidden', !(CMP.mode === 'wipe' && !CMP.hold));
  document.body.classList.toggle('cmp-wipe', CMP.mode === 'wipe' && !CMP.hold);
  if (CMP.mode === 'wipe') line.style.left = `calc(${CMP.split * 100}% - 1px)`;
  const showBadges = showOrig || CMP.mode === 'wipe';
  bO.classList.toggle('hidden', !showBadges);
  bN.classList.toggle('hidden', !showBadges);
}

function setCompareMode(m) {
  CMP.mode = (m === 'wipe' && CMP.mode === 'wipe') ? 'off' : m;
  CMP.hold = false;
  applyCompare();
}

// ---- 图层操作
async function layerOp(op, layerId) {
  busy(true, '处理中…');
  const r = await api('/api/ops', { ops: [Object.assign({ layer: layerId }, op)] });
  busy(false);
  if (!r.ok) return toast(r.error, true, 8000);
  applyState(r.project);
}
async function removeOpAt(layerId, idx) {
  busy(true, '回退中…');
  const r = await api('/api/ops', { ops: [{ op: 'remove_op', layer: layerId, index: idx }] });
  busy(false);
  if (!r.ok) return toast(r.error, true);
  if (SEL.layer === layerId && SEL.opIndex === idx) SEL.opIndex = null;
  applyState(r.project);
}

// ---- 打开图片 / 上传
let BR = { path: '', pick: new Set() };
async function browse(path) {
  const r = await api('/api/browse?path=' + encodeURIComponent(path || ''));
  if (!r.ok) return toast(r.error, true);
  BR.path = r.path;
  $('#brCrumb').textContent = r.path;
  $('#brDrives').innerHTML = (r.drives || []).map(d =>
    `<button class="ghost" data-drive="${d}">${d}</button>`).join(' ');
  const rows = [];
  if (r.parent) rows.push(`<div data-dir="${esc(r.parent)}"><span class="nm">📁 ..</span></div>`);
  (r.dirs || []).forEach(d => rows.push(`<div data-dir="${esc(d.path)}"><span class="nm">📁 ${esc(d.name)}</span></div>`));
  (r.files || []).forEach(f => rows.push(
    `<div data-file="${esc(f.path)}" class="${BR.pick.has(f.path) ? 'sel' : ''}">
       <span class="nm">${f.raw ? '🎞' : '🖼'} ${esc(f.name)}${f.raw ? ' <span class="tag vision">RAW</span>' : ''}</span>
       <span class="meta">${f.kb} KB · ${esc(f.mtime)}</span></div>`));
  $('#brList').innerHTML = rows.join('') || '<p class="hint">这个目录里没有图片</p>';
}

// ---- 设置面板事件（事件委托）
document.addEventListener('click', async (e) => {
  const t = e.target;
  const mdlBtn = t.closest('[data-mdl]');
  if (mdlBtn) {
    const act = mdlBtn.dataset.mdl, id = mdlBtn.dataset.id;
    const s = ST.settings;
    if (act === 'use') {
      const r = await api('/api/settings', { active_model: id });
      if (r.ok) { ST.settings = r.settings; fillModelPick(); render(); toast('已切换当前模型'); }
    } else if (act === 'teach') {
      const r = await api('/api/settings', { teaching_model: id });
      if (r.ok) { ST.settings = r.settings; render(); toast('已设为教学模型'); }
    } else if (act === 'test') {
      busy(true, '测试连通中…');
      const r = await api('/api/model/test', { model: id });
      busy(false);
      if (r.ok) toast(`连上了：${r.name}（${r.elapsed}s）返回「${r.sample}」`);
      else toast('测试失败：' + r.error, true, 9000);
    } else if (act === 'edit' || act === 'new') {
      openModelForm(act === 'edit' ? s.models.find(m => m.id === id) : null);
    } else if (act === 'reset') {
      if (!await askConfirm('恢复预设', '把模型清单恢复成内置预设？已填的密钥会被清掉。', '恢复')) return;
      const r = await api('/api/settings', { reset_models: true });
      if (r.ok) { ST.settings = r.settings; fillModelPick(); render(); toast('已恢复预设'); }
    }
    return;
  }
  const drv = t.closest('[data-drive]');
  if (drv) return browse(drv.dataset.drive);
  const dir = t.closest('[data-dir]');
  if (dir) { BR.pick.clear(); return browse(dir.dataset.dir); }
  const file = t.closest('[data-file]');
  if (file) {
    const p = file.dataset.file;
    if (e.ctrlKey) { BR.pick.has(p) ? BR.pick.delete(p) : BR.pick.add(p); }
    else { BR.pick.clear(); BR.pick.add(p); }
    $$('#brList [data-file]').forEach(d =>
      d.classList.toggle('sel', BR.pick.has(d.dataset.file)));
    return;
  }
  const openProj = t.closest('[data-open]');
  if (openProj) {
    busy(true, '载入工程…');
    const r = await api('/api/open', { paths: [openProj.dataset.open], mode: 'project' });
    busy(false);
    if (!r.ok) return toast(r.error, true);
    applyState(r.project); toast('已载入工程');
    return;
  }
  // 素材库：把素材加入图层（要放在图层处理之前，避免被当成管线步骤点击）
  const assetBtn = t.closest('[data-addasset]');
  if (assetBtn) {
    const row = assetBtn.closest('[data-asset]');
    const aid = row.dataset.asset;
    busy(true, '加入图层…');
    const r = await api('/api/ops', { ops: [{ op: 'add_layer', source: 'asset:' + aid }] });
    busy(false);
    if (!r.ok) return toast(r.error, true);
    toast('已加入图层');
    applyState(r.project);
    return;
  }
  // 长管线折叠行：点开该图层的全部步骤
  const foldRow = t.closest('[data-expand]');
  if (foldRow) {
    OPEN_OPS.add(foldRow.dataset.expand);
    renderLayers(ST.project);
    return;
  }
  // 素材行（非按钮区）：跳到用到它的第一个图层，方便核对"这张素材用在哪"
  const assetRow = t.closest('[data-asset][data-goto-layer]');
  if (assetRow) {
    SEL.layer = assetRow.dataset.gotoLayer;
    SEL.opIndex = null;
    renderLayers(ST.project);
    toast(`已切到图层 ${SEL.layer}（来自素材 ${assetRow.dataset.asset}）`);
    return;
  }
  const layerEl = t.closest('.layer');
  if (layerEl) {
    const lid = layerEl.dataset.layer;
    const act = t.dataset.act;
    if (t.dataset.del && t.closest('.oprow')) {
      const row = t.closest('.oprow');
      return removeOpAt(row.dataset.layer, Number(row.dataset.idx));
    }
    const rowEl = t.closest('.oprow');
    if (rowEl) {
      SEL.layer = rowEl.dataset.layer;
      SEL.opIndex = Number(rowEl.dataset.idx);
      const r = await api('/api/active', { layer: SEL.layer });
      if (r.ok) applyState(r.project); else render();
      return;
    }
    if (!act) return;
    if (act === 'active') {
      SEL.layer = lid; SEL.opIndex = null;
      const r = await api('/api/active', { layer: lid });
      if (r.ok) applyState(r.project); else render();
    } else if (act === 'vis') {
      const l = ST.project.layers.find(x => x.id === lid);
      layerOp({ op: 'set_layer', visible: !l.visible }, lid);
    } else if (act === 'up') layerOp({ op: 'reorder_layer', to: 'up' }, lid);
    else if (act === 'down') layerOp({ op: 'reorder_layer', to: 'down' }, lid);
    else if (act === 'del') {
      if (await askConfirm('删除图层', '删除图层 ' + lid + '？该图层及其全部调整步骤都会被移除。', '删除')) {
        layerOp({ op: 'delete_layer' }, lid);
      }
    } else if (act === 'cut') {
      if (!await askConfirm('一键抠图', '对本图层执行一键抠图？首次约需装载模型 5~10 秒。', '开始抠图')) return;
      layerOp({ op: 'remove_bg', engine: ST.settings.matting.engine || 'auto', feather: 1 }, lid);
    } else if (act === 'clearmask') layerOp({ op: 'clear_mask' }, lid);
    else if (act === 'fliph') {
      const l = ST.project.layers.find(x => x.id === lid);
      layerOp({ op: 'transform_layer', flip_h: !l.flip_h }, lid);
    } else if (act === 'flipv') {
      const l = ST.project.layers.find(x => x.id === lid);
      layerOp({ op: 'transform_layer', flip_v: !l.flip_v }, lid);
    } else if (act === 'rot90') {
      const l = ST.project.layers.find(x => x.id === lid);
      let a = (Number(l.rotation) + 90) % 360; if (a > 180) a -= 360;
      layerOp({ op: 'transform_layer', rotation: a }, lid);
    } else if (act === 'fit') layerOp({ op: 'fit_layer', mode: 'fit' }, lid);
    else if (act === 'fill') layerOp({ op: 'fit_layer', mode: 'fill' }, lid);
    else if (act === 'canvasfit') layerOp({ op: 'canvas', mode: 'fit' });
    else if (act === 'canvasratio') layerOp({ op: 'canvas_ratio', ratio: '1:1' });
    else if (act === 'center') layerOp({ op: 'align_layer', mode: 'center', ref: 'canvas' }, lid);
  }
});

// 图层属性输入
document.addEventListener('change', async (e) => {
  const t = e.target;
  if (t.id === 'teachModel') {
    const r = await api('/api/settings', { teaching_model: t.value });
    if (r.ok) { ST.settings = r.settings; render(); }
    return;
  }
  if (!t.dataset.act) return;
  const layerEl = t.closest('.layer');
  if (!layerEl) return;
  const lid = layerEl.dataset.layer;
  const act = t.dataset.act;
  if (act === 'opacity') return layerOp({ op: 'set_layer', opacity: Number(t.value) }, lid);
  if (act === 'blend') return layerOp({ op: 'set_layer', blend: t.value }, lid);
  if (['x', 'y', 'scale'].includes(act)) {
    const v = Number(t.value);
    if (!isFinite(v)) return;
    return layerOp({ op: 'transform_layer', [act]: v }, lid);
  }
});

// 微调滑杆：拖动即时生效（去抖），并保留可撤销
let tuneTimer = null;
document.addEventListener('input', (e) => {
  const t = e.target;
  if (!t.dataset.opKey) return;
  const val = Number(t.value);
  t.nextElementSibling.textContent = Math.round(val * 1000) / 1000;
  clearTimeout(tuneTimer);
  tuneTimer = setTimeout(async () => {
    const r = await api('/api/ops/update', {
      layer: t.dataset.layer, index: Number(t.dataset.idx), params: { [t.dataset.opKey]: val }
    });
    if (!r.ok) return toast(r.error, true);
    // 只换预览与图层信息，不重建面板（否则拖到一半 DOM 被替换、拖动会断）
    ST.project = r.project;
    const img = $('#preview');
    img.dataset.rev = r.project.rev;
    img.onload = fitPreview;
    img.src = r.project.preview_url;
    renderLayers(r.project);
    $('#layerInfo').textContent =
      `图层 ${r.project.layers.length} 个　当前 ${SEL.layer}`;
  }, 160);
});

// ---- 技能包 / 风格 / 网络（事件委托）
document.addEventListener('click', async (e) => {
  const t = e.target;
  const refRow = t.closest('[data-ref]');
  if (refRow) {
    const r = await api('/api/skills/' + encodeURIComponent(refRow.dataset.ref));
    if (!r.ok) return toast(r.error || '读取失败', true);
    $('#refTitle').textContent = `${r.title}（${r.chars} 字）`;
    $('#refBody').textContent = r.text;
    $('#refDlg').showModal();
    return;
  }
  if (t.id === 'skReload') {
    const r = await api('/api/skills/reload', {});
    if (!r.ok) return toast(r.error, true);
    toast(`已重新加载：${r.skills} 份参考、${r.styles} 套配方`);
    await refreshSkillsPanel();
    return;
  }
  if (t.id === 'styleApply') {
    if (!ST.project) return toast('请先打开图片', true);
    const layer = SEL.layer || ST.project.active;
    const strength = Number($('#styleStrength').value);
    busy(true, '套用风格中…');
    const r = await api('/api/ops', { ops: [{ op: 'apply_style', style: $('#stylePick').value,
      strength, layer }] });
    busy(false);
    if (!r.ok) return toast(r.error, true, 8000);
    toast(r.results[0] ? `${r.results[0].op} 完成` : '已套用');
    applyState(r.project);
    return;
  }
  if (t.id === 'cfSave') {
    const payload = {
      chat: { auto_refine: $('#cfRefine').checked, refine_max_ops: Number($('#cfRefineMax').value) },
      skills: { max_refs_per_turn: Number($('#cfRefs').value) }
    };
    const r = await api('/api/settings', payload);
    if (!r.ok) return toast(r.error, true);
    ST.settings = r.settings;
    $('#refineToggle').checked = ST.settings.chat.auto_refine;
    return toast('对话设置已保存');
  }
  if (t.id === 'nwSave') {
    const payload = { network: { use_proxy: $('#nwUse').checked, proxy: $('#nwProxy').value.trim() } };
    const r = await api('/api/settings', payload);
    if (!r.ok) return toast(r.error, true);
    ST.settings = r.settings;
    return toast(payload.network.use_proxy ? '已启用代理' : '已改为直连');
  }
  if (t.id === 'rawSave') {
    const r = await api('/api/settings', { raw: { decode: $('#rawMode').value } });
    if (!r.ok) return toast(r.error, true);
    ST.settings = r.settings;
    return toast('RAW 设置已保存');
  }
  if (t.id === 'nwProbe') {
    const btn = t;
    btn.disabled = true;
    $('#nwResult').textContent = '检测中…';
    const r = await api('/api/network/probe', { proxy: $('#nwProxy').value.trim() });
    btn.disabled = false;
    if (!r.ok) { $('#nwResult').textContent = r.error || '检测失败'; return; }
    const rows = (r.results || []).map(x => {
      const cls = v => /^HTTP (2|3|4)/.test(v) ? 'ok2' : 'bad2';
      return `<tr><td>${esc(x.target)}</td><td class="${cls(x.direct)}">${esc(x.direct)}</td>
        <td class="${cls(x.proxy)}">${esc(x.proxy)}</td></tr>`;
    }).join('');
    $('#nwResult').innerHTML =
      `代理 ${esc(r.proxy)} ${r.proxy_reachable ? '<span class="ok2">可连</span>' : '<span class="bad2">不可连</span>'}`
      + `<table class="probe"><tr><th>目标</th><th>直连</th><th>走代理</th></tr>${rows}</table>`
      + `<div class="hint">HTTP 2xx/3xx/4xx 都说明网络通（401/404 是正常的鉴权响应）。</div>`;
    return;
  }
});

document.addEventListener('change', (e) => {
  if (e.target.id === 'stylePick') {
    const s = STYLES.find(x => x.id === e.target.value);
    if (s) $('#styleIntent').innerHTML = `意图：${esc(s.intent)}`
      + (s.caution ? `<br><span style="color:var(--warn)">注意：${esc(s.caution)}</span>` : '');
  }
});
document.addEventListener('input', (e) => {
  if (e.target.id === 'styleStrength') $('#styleStrengthVal').textContent = e.target.value + '%';
  if (e.target.id === 'refineToggle') {
    api('/api/settings', { chat: { auto_refine: e.target.checked } }).then(r => {
      if (r && r.ok) { ST.settings = r.settings; toast(e.target.checked ? '已开启自动精修' : '已关闭自动精修'); }
    });
  }
});

// 顶部按钮
$('#btnUndo').onclick = doUndo;
$('#btnRedo').onclick = doRedo;
if ($('#btnZoom')) $('#btnZoom').onclick = cycleZoom;
if ($('#btnPixel')) $('#btnPixel').onclick = togglePixel;
if ($('#btnHelp')) $('#btnHelp').onclick = () => $('#helpDlg').showModal();

// 右侧面板宽度：拖动分隔条调整，记在本地（纯视图偏好，不进工程文件）
(() => {
  const grip = $('#panelGrip');
  if (!grip) return;
  const KEY = 'cog_panel_w';
  const saved = Number(localStorage.getItem(KEY) || 0);
  if (saved >= 280 && saved <= 720) document.documentElement.style.setProperty('--panel-w', saved + 'px');
  let dragging = false;
  grip.addEventListener('pointerdown', (e) => {
    dragging = true;
    grip.classList.add('active');
    try { grip.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
  });
  grip.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const w = Math.min(720, Math.max(280, window.innerWidth - e.clientX));
    document.documentElement.style.setProperty('--panel-w', w + 'px');
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    grip.classList.remove('active');
    const w = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--panel-w'), 10);
    if (w) localStorage.setItem(KEY, String(w));
    fitPreview();
  };
  grip.addEventListener('pointerup', stop);
  window.addEventListener('pointerup', stop);
})();

// 全局快捷键（输入框内不拦截，避免抢掉打字与文本编辑的 Ctrl+Z）
window.addEventListener('keydown', (e) => {
  const tag = (e.target && e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable;
  if (typing) {
    if (e.key === '?' && tag === 'textarea') return;      // 让用户能打问号
    return;
  }
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 'z' && !e.shiftKey) { e.preventDefault(); doUndo(); return; }
  if (mod && (e.key.toLowerCase() === 'y' || (e.key.toLowerCase() === 'z' && e.shiftKey))) {
    e.preventDefault(); doRedo(); return;
  }
  if (mod && e.key.toLowerCase() === 's') {
    e.preventDefault();
    if (ST.project) $('#btnExport').click();
    return;
  }
  if (e.key === '?') { e.preventDefault(); $('#helpDlg').showModal(); return; }
});

// ---- 原图对比：按钮 / 键盘 / 拖动分割线
$('#cmpHold').onclick = (e) => e.preventDefault();   // 纯瞬时控件，不做模式切换
$('#cmpWipe').onclick = () => setCompareMode('wipe');
$('#cmpOff').onclick = () => { CMP.mode = 'off'; CMP.hold = false; applyCompare(); };
$('#cmpExport').onclick = async () => {
  busy(true, '生成对比图…');
  const r = await api('/api/export/compare', { max_side: 2400, format: 'jpg', quality: 92 });
  busy(false);
  if (!r.ok) return toast(r.error, true, 8000);
  toast('对比图已导出：' + r.path, false, 9000);
};
// 按住看原图：**按下即生效，与当前模式无关**（曾经要求先点一下切换模式，
// 导致"第一次按住没反应"）。鼠标按住按钮、在图上按住、或按住 \ 键都可以。
(() => {
  const b = $('#cmpHold'), stack = $('#stack');
  const down = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    CMP.hold = true;
    applyCompare();
  };
  const up = () => { if (CMP.hold) { CMP.hold = false; applyCompare(); } };
  b.addEventListener('pointerdown', (e) => { e.preventDefault(); down(e); });
  // 在图上直接按住也能看原图（拖动分割线时除外）
  stack.addEventListener('pointerdown', (e) => {
    if (PIXEL) return;                      // 输出像素模式下是拖动平移，不是按住看原图
    if (CMP.mode === 'wipe') return;
    if (e.target.closest('#cmpHold')) return;
    e.preventDefault();
    down(e);
  });
  window.addEventListener('pointerup', up);
  window.addEventListener('blur', up);
  window.addEventListener('keydown', (e) => {
    if (e.key === '\\' && !e.repeat && !/input|textarea|select/i.test(e.target.tagName)) {
      e.preventDefault();
      CMP.hold = true;
      applyCompare();
    }
  });
  window.addEventListener('keyup', (e) => { if (e.key === '\\') up(); });
})();
// 擦除模式：在图上左右拖动即可移动分割线。
// 必须挡住浏览器把 <img> 当成可拖拽对象——否则拖到左半区会抓起"原图"那张，
// 松手时被窗口的 drop 处理器当成新素材叠到画布上（用户实际遇到的问题）。
(() => {
  const stack = $('#stack');
  let dragging = false;
  const move = (e) => {
    if (!dragging || CMP.mode !== 'wipe') return;
    const r = stack.getBoundingClientRect();
    CMP.split = Math.min(0.99, Math.max(0.01, (e.clientX - r.left) / r.width));
    applyCompare();
  };
  stack.addEventListener('pointerdown', (e) => {
    if (CMP.mode !== 'wipe') return;
    e.preventDefault();                       // 阻止原生图片拖拽/文字选择
    dragging = true;
    try { stack.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    move(e);
  });
  stack.addEventListener('pointermove', move);
  stack.addEventListener('dragstart', (e) => e.preventDefault());
  window.addEventListener('pointerup', () => { dragging = false; });
})();
$('#btnSend').onclick = sendChat;
$('#chatInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$$('.quick button').forEach(b => b.onclick = () => {
  $('#chatInput').value = b.dataset.fill;
  $('#chatInput').focus();
});
$('#btnOpen').onclick = () => { BR.pick.clear(); browse(''); $('#openDlg').showModal(); };
// 空状态里的主按钮，等同于顶栏「打开图片」
if ($('#hintOpen')) $('#hintOpen').onclick = () => $('#btnOpen').click();
$('#btnAdd').onclick = () => { BR.pick.clear(); browse(''); $('#openDlg').showModal(); };
$('#brUp').onclick = () => { const up = BR.path.replace(/[\\/][^\\/]*$/, ''); browse(up || BR.path); };
$('#brNew').onclick = async () => {
  const paths = Array.from(BR.pick);
  if (!paths.length) return toast('请先在上面的列表里点选图片（Ctrl+点击可多选）', true);
  $('#openDlg').close();
  busy(true, '建立工程…');
  const r = await api('/api/open', { paths, mode: 'new' });
  busy(false);
  if (!r.ok) return toast(r.error, true);
  SEL = { layer: null, opIndex: null };
  applyState(r.project); toast('工程已建立');
};
$('#brAdd').onclick = async () => {
  const paths = Array.from(BR.pick);
  if (!paths.length) return toast('请先点选图片', true);
  if (!ST.project) { $('#brNew').click(); return; }
  $('#openDlg').close();
  busy(true, '加入图层…');
  const r = await api('/api/open', { paths, mode: 'layer' });
  busy(false);
  if (!r.ok) return toast(r.error, true);
  applyState(r.project); toast('已加入 ' + (r.added || []).length + ' 个图层');
};
// 导出参数记忆：连出多张图时不必每次重填质量与路径（只存本地，不进工程文件）
const EX_KEY = 'cog_export_prefs';
function exPrefs() {
  try { return JSON.parse(localStorage.getItem(EX_KEY) || '{}') || {}; } catch (e) { return {}; }
}
$('#btnExport').onclick = () => {
  const d = exPrefs();
  if (d.format) $('#exFmt').value = d.format;
  if (d.quality) $('#exQ').value = d.quality;
  if (d.max_side != null) $('#exMax').value = d.max_side;
  if (d.dir) $('#exPath').value = d.dir;
  $('#exportDlg').showModal();
};
$('#exGo').onclick = async () => {
  $('#exportDlg').close();
  busy(true, '导出中…');
  const r = await api('/api/export', {
    format: $('#exFmt').value, quality: Number($('#exQ').value),
    max_side: Number($('#exMax').value), path: $('#exPath').value.trim()
  });
  busy(false);
  if (!r.ok) return toast(r.error, true);
  try {
    localStorage.setItem(EX_KEY, JSON.stringify({
      format: $('#exFmt').value, quality: Number($('#exQ').value),
      max_side: Number($('#exMax').value),
      dir: String(r.path || '').replace(/[\\/][^\\/]*$/, ''),   // 记住上次的目录，不记文件名
    }));
  } catch (e) { /* 本地存储不可用时忽略 */ }
  toast('已导出：' + r.path, false, 9000);
  applyState(r.project);
};
$('#btnQuit').onclick = async () => {
  const yes = await askConfirm('关闭服务',
    '关闭本机的机魂修图台服务？\n\n工程已自动保存，下次双击启动脚本即可继续。', '关闭服务');
  if (!yes) return;
  // 先把反馈显示出来再发请求：即使服务立刻断开，用户也一定看得到发生了什么
  showFarewell('正在退出…');
  const r = await api('/api/shutdown', {});
  if (r && r.ok) {
    showFarewell('服务已退出，可以直接关闭这个页面。');
  } else {
    showFarewell('未能确认退出：' + ((r && r.error) || '未知原因')
      + '\n\n若服务仍在运行，请在启动它的那个黑色窗口里按 Ctrl+C，或直接关闭该窗口。');
  }
};
function showFarewell(msg) {
  document.body.innerHTML =
    '<div style="padding:56px;max-width:760px;margin:0 auto;color:#c8cddb;font-size:15px;line-height:1.9">'
    + esc(msg).replace(/\n/g, '<br>') + '</div>';
}

// 教学模式
let teachOn = false;
// 教学看哪张图：original=导入的原片（评拍摄水平）；current=画布成图（评修图效果）
function teachTarget() {
  const s = (ST && ST.settings && ST.settings.teaching) || {};
  return s.target === 'current' ? 'current' : 'original';
}
function renderTeachBar() {
  const el = $('#teachTarget');
  if (!el) return;
  const cur = teachTarget();
  el.innerHTML = `<div class="segbar">
    <button data-tt="original" class="${cur === 'original' ? 'on' : ''}"
      title="送未修的原片：评价你的拍摄水平（测光/构图/用光/机位）">看原图 · 评拍摄</button>
    <button data-tt="current" class="${cur === 'current' ? 'on' : ''}"
      title="送画布上的成图：评价修图效果（含你已做的调色/裁切/抠图）">看当前成图 · 评修图</button>
  </div>
  <div class="hint" style="margin-top:4px">${
    cur === 'original'
      ? '当前：原图（未修）。注意若你已裁切，画幅与你现在的工作不一致。'
      : '当前：成图（与你看到的一致）。'}</div>`;
  el.querySelectorAll('[data-tt]').forEach(b => {
    b.onclick = async () => {
      const r = await api('/api/settings', { teaching: { target: b.dataset.tt } });
      if (r.ok) { ST.settings = r.settings; renderTeachBar(); toast('教学模式改为：'
        + (b.dataset.tt === 'current' ? '看当前成图' : '看原图')); }
      else toast(r.error, true);
    };
  });
}
$('#btnTeach').onclick = async () => {
  teachOn = !teachOn;
  $('#btnTeach').classList.toggle('on', teachOn);
  if (!teachOn) { $$('.tabs button')[2].click(); return; }
  $$('.tabs button')[2].click();
  if (!ST.project) return toast('请先打开图片', true);
  await runTeaching();
};
$('#btnTuner').onclick = () => {
  const t = $('#tuner');
  t.classList.toggle('show');
  if (t.classList.contains('show')) renderTuner(ST.project);
};
async function runTeaching(applyEdits = false) {
  const box = $('#teachBody');
  const target = teachTarget();
  box.innerHTML = `<div class="hint">讲师正在看${target === 'current' ? '当前成图' : '原图'}…（首次可能需要十几秒）</div>`;
  busy(true, '讲师正在看照片…');
  const r = await api('/api/teaching', { target, apply_edits: applyEdits });
  busy(false);
  if (!r.ok) { box.innerHTML = `<pre class="err">${esc(r.error)}</pre>`; return; }
  const c = r.critique;
  if (applyEdits) applyState(r.project);
  const names = c.score_labels || {};
  const order = ['overall', 'exposure', 'focus', 'color', 'composition', 'lighting', 'impact'];
  box.innerHTML = `
    <div class="card"><h4>总评</h4>
      <div class="hint" style="margin-bottom:6px">本次评价的是：<b style="color:var(--accent)">${
        c.target === 'current' ? '当前成图（含你的修图）' : '原始照片（未修）'}</b></div>
      <div>${esc(c.summary || '（无）')}</div>
      <div class="hint" style="margin-top:6px">模型 ${esc(c.model)} · ${c.elapsed}s</div>
      ${c.parse_note ? `<div class="hint" style="color:var(--accent);margin-top:4px">${esc(c.parse_note)}</div>` : ''}</div>
    <div class="card"><h4>分项评分</h4>
      ${order.filter(k => c.scores[k] != null).map(k => `
        <div class="score"><span class="nm">${esc(names[k] || k)}</span>
          <span class="bar"><i style="width:${c.scores[k] * 10}%"></i></span>
          <span class="v">${c.scores[k]}</span></div>`).join('') || '<div class="hint">模型没有给出分数</div>'}
    </div>
    ${c.strengths.length ? `<div class="card"><h4>做对的地方</h4><ul>${c.strengths.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
    ${c.issues.length ? `<div class="card"><h4>问题与补救</h4>${c.issues.map(i => `
      <div class="issue"><b>${esc(i.title)}</b><div>${esc(i.detail)}</div>
      ${i.fix ? `<div class="fix">→ ${esc(i.fix)}</div>` : ''}</div>`).join('')}</div>` : ''}
    ${c.shooting_tips.length ? `<div class="card"><h4>下次拍摄清单</h4><ul>${c.shooting_tips.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
    ${c.edits && c.edits.length ? `<div class="card"><h4>可一键应用的修图</h4>
      ${c.edits.map(o => `<div class="op">▸ ${esc(o.op)} ${esc(opSummary(o))}</div>`).join('')}
      ${c.edits_note ? `<div class="hint">${esc(c.edits_note)}</div>` : ''}
      ${c.applied && c.applied.length ? '<div class="hint" style="color:var(--ok)">已应用</div>'
        : '<button id="applyTeach" class="primary" style="margin-top:8px">应用这些修图</button>'}</div>` : ''}
    <div class="hint">教学模式按你的选择只依据视觉模型读图；若当前模型不支持读图，请到「设置」里换一个可读图的模型。</div>`;
  const btn = $('#applyTeach');
  if (btn) btn.onclick = () => runTeaching(true);
}

// 拖拽与粘贴
window.addEventListener('dragover', e => { e.preventDefault(); const st = $('#stage'); if (st) st.classList.add('dropping'); });
// 拖放落点高亮（只影响视觉，不改 drop 的判定逻辑）
['dragleave', 'drop', 'dragend'].forEach(ev =>
  window.addEventListener(ev, () => { const st = $('#stage'); if (st) st.classList.remove('dropping'); }));
window.addEventListener('drop', async e => {
  e.preventDefault();
  // 只接受**来自系统**的文件拖放。页面内图片拖拽（例如拖动预览图看细节）在 Chrome 里
  // 也会把该图塞进 dataTransfer.files —— 若不拦住，成品预览就会被当成新素材叠到画布上。
  if (window.__internalDrag) { window.__internalDrag = false; return; }
  const dt = e.dataTransfer;
  const types = Array.from((dt && dt.types) || []);
  const fromOS = types.includes('Files') && !types.includes('text/html') && !types.includes('text/uri-list');
  if (!fromOS) return;
  const files = Array.from((dt && dt.files) || []).filter(f => f.type.startsWith('image/'));
  if (!files.length) return;
  await uploadFiles(files, e.shiftKey ? 'layer' : (ST.project ? 'layer' : 'new'));
});
// 页面内开始的拖拽一律标记为"内部拖拽"（与上面的判定互为双保险）
window.addEventListener('dragstart', () => { window.__internalDrag = true; }, true);
window.addEventListener('dragend', () => { setTimeout(() => { window.__internalDrag = false; }, 0); }, true);
window.addEventListener('paste', async e => {
  const items = Array.from(e.clipboardData?.items || []);
  const files = items.filter(i => i.type.startsWith('image/')).map(i => i.getAsFile()).filter(Boolean);
  if (!files.length) return;
  await uploadFiles(files, ST.project ? 'layer' : 'new');
});
async function uploadFiles(files, mode) {
  busy(true, '导入图片…');
  const payload = [];
  for (const f of files) {
    const dataUrl = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result); r.onerror = rej;
      r.readAsDataURL(f);
    });
    payload.push({ name: f.name, data_url: dataUrl });
  }
  const r = await api('/api/upload', { files: payload, mode });
  busy(false);
  if (!r.ok) return toast(r.error, true, 8000);
  if (mode === 'new') SEL = { layer: null, opIndex: null };
  applyState(r.project);
  toast('已导入 ' + files.length + ' 张图片');
}

// ---------------------------------------------------------------- 模型编辑表单
function openModelForm(m) {
  const isNew = !m;
  const d = m || { id: '', name: '', base_url: 'https://api.deepseek.com', model: 'deepseek-flash',
    vision: true, json_mode: true, temperature: 0.2, max_tokens: 4096, api_key: '', api_key_env: '' };
  $('#mdlForm').innerHTML = `
  <div class="card" style="margin-top:10px">
    <h4>${isNew ? '新增模型' : '编辑：' + esc(d.name || d.id)}</h4>
    <div class="grid2">
      <div><label>标识 id（不可重复，英文）</label><input id="mfId" value="${esc(d.id)}" ${isNew ? '' : 'readonly'}></div>
      <div><label>显示名</label><input id="mfName" value="${esc(d.name || '')}"></div>
    </div>
    <label>base_url（OpenAI 兼容端点，末尾不要带 /chat/completions）</label>
    <input id="mfUrl" value="${esc(d.base_url || '')}" placeholder="https://api.deepseek.com">
    <div class="grid2">
      <div><label>模型名</label><input id="mfModel" value="${esc(d.model || '')}" placeholder="deepseek-flash"></div>
      <div><label>读图能力</label>
        <select id="mfVision">
          <option value="1" ${d.vision ? 'selected' : ''}>支持读图（可用于教学）</option>
          <option value="0" ${d.vision ? '' : 'selected'}>纯文本</option>
        </select></div>
    </div>
    <label>API Key（只存在本机 config/settings.json）</label>
    <input id="mfKey" type="password" value="${esc(d.api_key || '')}" placeholder="留空则读环境变量">
    <div class="grid3">
      <div><label>密钥环境变量名</label><input id="mfEnv" value="${esc(d.api_key_env || '')}"></div>
      <div><label>temperature</label><input id="mfTemp" type="number" step="0.1" value="${d.temperature}"></div>
      <div><label>max_tokens</label><input id="mfMax" type="number" value="${d.max_tokens}"></div>
    </div>
    <label><input type="checkbox" id="mfJson" ${d.json_mode ? 'checked' : ''} style="width:auto"> 使用 JSON 输出模式（推荐）</label>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button id="mfSave" class="primary">保存</button>
      <button id="mfCancel" class="ghost">取消</button>
    </div>
  </div>`;
  $('#mfCancel').onclick = () => { $('#mdlForm').innerHTML = ''; };
  $('#mfSave').onclick = async () => {
    const id = $('#mfId').value.trim() || 'custom';
    const model = {
      id, name: $('#mfName').value.trim() || id, base_url: $('#mfUrl').value.trim().replace(/\/+$/, ''),
      model: $('#mfModel').value.trim(), api_key: $('#mfKey').value,
      api_key_env: $('#mfEnv').value.trim(), vision: $('#mfVision').value === '1',
      json_mode: $('#mfJson').checked, temperature: Number($('#mfTemp').value),
      max_tokens: Number($('#mfMax').value)
    };
    const r = await api('/api/settings', { model });
    if (!r.ok) return toast(r.error, true);
    ST.settings = r.settings; fillModelPick(); render();
    $('#mdlForm').innerHTML = '';
    toast('已保存模型 ' + model.name);
  };
}

// ---------------------------------------------------------------- 启动
(async function boot() {
  const r = await api('/api/state');
  if (!r.ok) { toast(r.error || '无法连接本地服务', true); return; }
  ST = r;
  fillModelPick();
  const m = await api('/api/ops/manual');
  if (m.ok) {
    MANUAL = m;
    m.specs.forEach(s => SPECS[s.name] = s);
    STYLES = m.style_list || [];
  }
  const rt = $('#refineToggle');
  if (rt) rt.checked = !!(ST.settings && ST.settings.chat && ST.settings.chat.auto_refine);
  if (ST.project) applyState(ST.project); else render();
  // 深链：?cmp=wipe|hold 直接进入对比状态（也方便截图与出报告）
  const cmp = new URLSearchParams(location.search).get('cmp');
  if (cmp && ['wipe', 'hold'].includes(cmp) && cmpAvailable()) {
    if (cmp === 'hold') { CMP.hold = true; } else { CMP.mode = cmp; }
    applyCompare();
  }
  const hash = location.hash.slice(1);
  if (['chat', 'layers', 'teach', 'settings'].includes(hash)) switchTab(hash);
  if (ST.settings && !ST.settings.models_with_key.length) {
    toast('还没配置模型密钥：打开「设置」填入 DeepSeek API Key 后即可用自然语言修图', false, 9000);
  }
  setInterval(async () => {   // 轻量心跳：服务被关掉时给出提示
    const h = await api('/api/health');
    if (!h || !h.ok) toast('本地服务已断开，请重新启动', true, 9000);
  }, 20000);
})();
