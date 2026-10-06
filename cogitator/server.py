"""本地 HTTP 服务：浏览器界面 + JSON API。

只监听 127.0.0.1（仅本机可访问）。长任务（抠图、模型调用）走同步端点，
由 FastAPI 放进线程池执行，不会卡住事件循环；改动文档的路径统一加锁。
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import (APP_NAME, __version__, agent as agent_mod, llm, matting, ops, raw_io, render,
               skills, styles as styles_mod, teaching as teach_mod)
from .config import ModelProfile, Settings, preset_models
from .document import ASSET_EXT, DocError, Project
from .paths import LOG_DIR, PROJECTS_DIR, WEB_DIR, ensure_dirs, safe_name

LOCK = threading.RLock()
# 由 __main__ 注入 uvicorn Server，供 /api/shutdown 优雅退出（避免响应与退出抢跑）
_UVICORN: dict = {"server": None}


class State:
    def __init__(self) -> None:
        ensure_dirs()
        self.settings = Settings.load()
        self.project: Project | None = None

    def require(self) -> Project:
        if self.project is None:
            raise HTTPException(409, "还没有打开工程：请先「打开图片」新建工程，或从最近的工程里选一个。")
        return self.project


STATE = State()


def log_event(kind: str, **fields: Any) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, **fields}
        with (LOG_DIR / "app.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


app = FastAPI(title=APP_NAME, version=__version__, docs_url=None, redoc_url=None)


@app.middleware("http")
async def _charset_middleware(request, call_next):
    """给 JSON 响应显式带上 charset，老客户端（如 Windows PowerShell）才不会按 Latin-1 解码成乱码。"""
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json") and "charset" not in ct:
        resp.headers["content-type"] = "application/json; charset=utf-8"
    return resp


@app.exception_handler(DocError)
def _doc_error(_req, exc: DocError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.exception_handler(ops.OpError)
def _op_error(_req, exc: ops.OpError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.exception_handler(llm.LLMError)
def _llm_error(_req, exc: llm.LLMError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ---------------------------------------------------------------- 静态界面

@app.get("/", response_class=HTMLResponse)
def index() -> Response:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__VERSION__", __version__))


@app.get("/web/{name:path}")
def web_asset(name: str) -> Response:
    p = (WEB_DIR / name).resolve()
    if not str(p).startswith(str(WEB_DIR.resolve())) or not p.is_file():
        raise HTTPException(404, "not found")
    suffix = p.suffix.lower()
    media = {".js": "application/javascript; charset=utf-8",
             ".css": "text/css; charset=utf-8",
             ".html": "text/html; charset=utf-8",
             ".svg": "image/svg+xml"}.get(suffix, "application/octet-stream")
    # 禁缓存：否则改了前端后浏览器仍用旧 JS，会出现"点了没反应"这类幽灵问题（排查成本极高）
    return Response(p.read_bytes(), media_type=media,
                    headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})


@app.get("/project/preview")
def preview() -> Response:
    proj = STATE.require()
    p = proj.preview_path()
    if not p.is_file():
        proj._push_preview()
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/project/original")
def project_original() -> Response:
    """原图基准（只含几何校正、未调色）—— 与预览同尺寸，供擦除/闪烁对比逐像素对齐。"""
    proj = STATE.require()
    scale = proj.preview_scale()
    rgb, alpha = proj.render_original(scale=scale)
    pil = render.to_pil(render.flatten_on(rgb, alpha), None)
    bio = io.BytesIO()
    pil.save(bio, format="JPEG", quality=int(proj.settings.preview_quality) if proj.settings else 88,
             optimize=True)
    return Response(bio.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/project/source/{layer_id}")
def layer_source(layer_id: str, thumb: int = Query(0, ge=0, le=4000)) -> Response:
    proj = STATE.require()
    layer = proj.layer(layer_id)
    p = (proj.dir / layer["source"]).resolve()
    if not p.is_file() or not str(p).startswith(str(proj.dir.resolve())):
        raise HTTPException(404, "source not found")
    if thumb:
        from PIL import Image

        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((thumb, thumb))
            bio = io.BytesIO()
            im.save(bio, format="JPEG", quality=85)
        return Response(bio.getvalue(), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    return FileResponse(p, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- 工程

@app.get("/api/state")
def api_state() -> dict:
    if STATE.project is None:
        return {"ok": True, "project": None, "settings": STATE.settings.public(),
                "recent": _recent_projects()}
    return {"ok": True, "project": STATE.project.state(),
            "settings": STATE.settings.public(), "recent": _recent_projects()}


def _recent_projects(limit: int = 12) -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    items: list[tuple[float, dict]] = []
    for d in PROJECTS_DIR.iterdir():
        f = d / "document.json"
        if not f.is_file():
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        items.append((f.stat().st_mtime, {
            "dir": str(d), "name": doc.get("name", d.name),
            "size": f"{doc.get('canvas', {}).get('w')}x{doc.get('canvas', {}).get('h')}",
            "mtime": time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
            "preview": f"file:///{(d / 'previews' / 'current.jpg').as_posix()}",
        }))
    items.sort(key=lambda t: -t[0])
    return [x[1] for x in items[:limit]]


@app.get("/api/browse")
def api_browse(path: str = Query("")) -> dict:
    """极简文件浏览：列出目录与其中的图片，供界面选图。"""
    if not path:
        home = Path.home()
        for cand in (home / "Pictures", home / "Desktop", home):
            if cand.is_dir():
                path = str(cand)
                break
    p = Path(path)
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        raise HTTPException(400, f"目录不存在：{path}")
    dirs, files = [], []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("$") or child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
                elif child.suffix.lower() in ASSET_EXT:
                    st = child.stat()
                    files.append({"name": child.name, "path": str(child),
                                  "kb": int(st.st_size / 1024),
                                  "raw": raw_io.is_raw(child),
                                  "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"没有权限读取：{p}") from None
    return {"ok": True, "path": str(p), "parent": str(p.parent) if p.parent != p else "",
            "dirs": dirs[:200], "files": files[:400],
            "drives": [f"{c}:\\" for c in "CDEFGH" if Path(f"{c}:\\").exists()]}


@app.post("/api/open")
def api_open(payload: dict = Body(...)) -> dict:
    paths = [str(x) for x in (payload.get("paths") or []) if str(x).strip()]
    mode = str(payload.get("mode") or "new")
    name = str(payload.get("name") or "")
    if not paths:
        raise HTTPException(400, "没有选择图片")
    with LOCK:
        if mode == "layer" and STATE.project is not None:
            proj = STATE.project
            proj._snapshot()
            added = []
            cw, ch = int(proj.doc["canvas"]["w"]), int(proj.doc["canvas"]["h"])
            for p in paths:
                asset = proj.import_asset(p)
                layer = proj._new_layer(asset, cw / 2.0, ch / 2.0, Path(p).stem)
                # 新加入的图层默认按画布适配，避免尺寸差异过大
                lw, lh = layer["src_size"]
                if lw > 0 and lh > 0:
                    layer["scale"] = float(min(cw / lw, ch / lh))
                added.append(layer["id"])
            proj.doc["active"] = added[-1] if added else proj.doc.get("active", "")
            proj.doc["rev"] = int(proj.doc.get("rev", 0)) + 1
            proj.save()
            proj._push_preview()
            log_event("open", mode=mode, added=added)
            return {"ok": True, "project": proj.state(), "added": added}
        if mode == "project" and len(paths) == 1 and (Path(paths[0]) / "document.json").is_file():
            STATE.project = Project.load(Path(paths[0]), STATE.settings)
            log_event("open", mode="project", dir=paths[0])
            return {"ok": True, "project": STATE.project.state()}
        STATE.project = Project.create_from_images(paths, STATE.settings, name=name)
        log_event("open", mode="new", dir=str(STATE.project.dir), count=len(paths))
        return {"ok": True, "project": STATE.project.state()}


@app.post("/api/upload")
def api_upload(payload: dict = Body(...)) -> dict:
    """浏览器拖拽/粘贴的图片：base64 → 落到工程 sources/ 或新建工程。"""
    items = payload.get("files") or []
    if not items:
        raise HTTPException(400, "没有收到图片数据")
    mode = str(payload.get("mode") or "new")
    tmp_dir = PROJECTS_DIR / "_inbox"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, item in enumerate(items):
        # 界面上传用 data_url（完整 data URL）；也容忍直接给 base64 的 data。
        data = str(item.get("data_url") or item.get("data") or "")
        if "," in data:
            data = data.split(",", 1)[1]
        try:
            raw = base64.b64decode(data)
        except Exception:
            raise HTTPException(400, f"第 {i + 1} 张图片的 base64 数据无法解码") from None
        if not raw:
            # 明确报错，而不是写个 0 字节文件让后面 Image.open 抛 500
            raise HTTPException(400, f"第 {i + 1} 张图片的数据为空（缺少 data_url/data 字段？）")
        name0 = str(item.get("name") or f"upload_{i}.png")
        fname = safe_name(Path(name0).stem) + (Path(name0).suffix or ".png")
        out = tmp_dir / f"{int(time.time() * 1000)}_{i}_{fname}"
        out.write_bytes(raw)
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(out) as im:
                im.verify()
        except (UnidentifiedImageError, OSError) as e:
            # 只把"图片本身有问题"当成 400。其它异常（例如拼写错误导致的 NameError）
            # 必须继续抛出去变成 500——曾经因为宽泛的 except Exception 掩盖了一个 NameError，
            # 结果把"服务端 bug"误报成"你上传的不是图片"。
            raise HTTPException(400, f"第 {i + 1} 张不是可识别的图片：{name0}（{e}）") from None
        saved.append(str(out))
    try:
        result = api_open({"paths": saved, "mode": mode, "name": payload.get("name") or ""})
    finally:
        for p in saved:                       # 中转文件已复制进工程，删掉避免堆积
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    return result


@app.post("/api/active")
def api_active(payload: dict = Body(...)) -> dict:
    proj = STATE.require()
    with LOCK:
        proj.set_active(str(payload.get("layer") or ""))
    return {"ok": True, "project": proj.state()}


@app.get("/api/ops/manual")
def api_manual() -> dict:
    cats = ["调色", "几何", "抠图", "图层", "画布", "输出"]
    return {"ok": True, "manual": ops.manual(),
            "styles": styles_mod.manual(),
            "style_list": [s.to_dict() for s in styles_mod.load_styles()],
            "specs": [{"name": s.name, "category": s.category, "desc": s.desc,
                       "kind": s.kind, "layer": s.layer,
                       "params": [{"name": p.name, "kind": p.kind, "desc": p.desc,
                                   "default": p.default, "min": p.lo, "max": p.hi,
                                   "enum": p.enum, "required": p.required,
                                   "optional": p.optional} for p in s.params]}
                      for s in ops.SPECS],
            "categories": cats}


# ---------------------------------------------------------------- 技能包 / 风格 / 网络

@app.get("/api/skills")
def api_skills() -> dict:
    st = skills.status(STATE.settings)
    st["refs"] = [{"id": r.id, "title": r.title, "summary": r.summary, "pack": p.id}
                  for p, r in skills.all_refs(STATE.settings)]
    return {"ok": True, **st}


@app.get("/api/skills/{ref_id}")
def api_skill_ref(ref_id: str) -> dict:
    pairs = skills.resolve([ref_id], STATE.settings)
    if not pairs:
        raise HTTPException(404, f"没有找到参考：{ref_id}")
    _pack, ref = pairs[0]
    text = ref.path.read_text(encoding="utf-8")
    return {"ok": True, "id": ref.id, "title": ref.title, "summary": ref.summary,
            "text": text, "chars": len(text)}


@app.post("/api/skills/reload")
def api_skills_reload() -> dict:
    skills.reload()
    styles_mod.load_styles(force=True)
    return {"ok": True, "skills": skills.status(STATE.settings)["reference_count"],
            "styles": len(styles_mod.load_styles())}


@app.get("/api/styles")
def api_styles() -> dict:
    return {"ok": True, "styles": [s.to_dict() for s in styles_mod.load_styles()],
            "manual": styles_mod.manual()}


@app.post("/api/network/probe")
def api_network_probe(payload: dict | None = Body(default=None)) -> dict:
    """探测直连与代理对几个关键端点的可达性（设置页的「检测网络」按钮）。"""
    import socket

    net = STATE.settings.network
    proxy = str((payload or {}).get("proxy") or net.get("proxy") or "http://127.0.0.1:7897")
    hostport = proxy.split("//")[-1]
    targets = [
        ("https://api.deepseek.com/v1/models", "DeepSeek"),
        ("https://huggingface.co/api/models?limit=1", "HuggingFace"),
        ("https://api.openai.com/v1/models", "OpenAI"),
    ]

    def one(url: str, use_proxy: bool) -> str:
        try:
            kw: dict[str, Any] = {"timeout": 12, "follow_redirects": True}
            if use_proxy:
                kw["proxy"] = proxy
            with httpx.Client(**kw) as c:
                r = c.get(url)
            return f"HTTP {r.status_code}"
        except Exception as e:
            return f"{type(e).__name__}"

    tcp_ok = False
    try:
        h, prt = hostport.split(":")
        with socket.create_connection((h, int(prt)), timeout=2.5):
            tcp_ok = True
    except Exception:
        tcp_ok = False
    return {"ok": True, "proxy": proxy, "proxy_reachable": tcp_ok,
            "results": [{"target": label, "direct": one(url, False),
                         "proxy": one(url, True) if tcp_ok else "（代理不可连）"}
                        for url, label in targets]}


# ---------------------------------------------------------------- 编辑

@app.post("/api/ops")
def api_ops(payload: dict = Body(...)) -> dict:
    proj = STATE.require()
    with LOCK:
        results = proj.apply_ops(payload.get("ops"))
    return {"ok": True, "results": results, "project": proj.state()}


@app.post("/api/ops/update")
def api_ops_update(payload: dict = Body(...)) -> dict:
    proj = STATE.require()
    with LOCK:
        updated = proj.update_op(str(payload.get("layer") or ""), int(payload.get("index", -1)),
                                 payload.get("params") or {})
    return {"ok": True, "op": updated, "project": proj.state()}


@app.post("/api/undo")
def api_undo() -> dict:
    proj = STATE.require()
    with LOCK:
        changed = proj.undo_step()
    return {"ok": True, "changed": changed, "project": proj.state()}


@app.post("/api/redo")
def api_redo() -> dict:
    proj = STATE.require()
    with LOCK:
        changed = proj.redo_step()
    return {"ok": True, "changed": changed, "project": proj.state()}


@app.post("/api/export/compare")
def api_export_compare(payload: dict | None = Body(default=None)) -> dict:
    """导出「原图 | 成品」对比图（含修改步骤清单）。"""
    proj = STATE.require()
    p = payload or {}
    with LOCK:
        if not proj.is_edited():
            raise HTTPException(400, "这张图还没有调色类修改，无需对比图。")
        out = proj.export_compare(max_side=int(p.get("max_side") or 2400),
                                  fmt=str(p.get("format") or "jpg"),
                                  quality=int(p.get("quality") or 92))
    log_event("export_compare", path=str(out))
    return {"ok": True, "path": str(out), "summary": proj.edit_summary()}


@app.post("/api/export")
def api_export(payload: dict = Body(...)) -> dict:
    proj = STATE.require()
    with LOCK:
        path = proj.export(str(payload.get("format") or "png"),
                           int(payload.get("quality") or 95),
                           int(payload.get("max_side") or 0),
                           payload.get("path") or None)
        proj.last_export = str(path)
        proj.save()
    return {"ok": True, "path": str(path), "project": proj.state()}


# ---------------------------------------------------------------- 对话 / 教学

@app.post("/api/chat")
def api_chat(payload: dict = Body(...)) -> dict:
    proj = STATE.require()
    text = str(payload.get("message") or "")
    refine = payload.get("refine")
    with LOCK:
        reply = agent_mod.ImageAgent(proj, STATE.settings).ask(
            text, refine=None if refine is None else bool(refine))
    log_event("chat", chars=len(text), applied=len(reply.applied), model=reply.model,
              loaded=reply.loaded, refined=bool(reply.refine))
    return {"ok": True, "reply": reply.to_dict(), "project": proj.state()}


# 全分辨率渲染缓存：{"key": (工程目录, rev), "rgb": uint8, "alpha": uint8}
# 为什么缓存：24MP 全分辨率渲染要数秒，而"输出像素查看"需要拖动平移——
# 按 rev 缓存住这一次渲染，之后的平移只是从缓存里裁剪 + JPEG 编码（几十毫秒）。
_FULL_CACHE: dict = {"key": None, "rgb": None, "alpha": None}


@app.get("/project/view")
def project_view(x: int = 0, y: int = 0, w: int = 1200, h: int = 800) -> Response:
    """按**输出像素**查看：全分辨率渲染后裁出请求区域（1:1 供判断锐度与噪点）。

    预览是按显示尺寸渲染的下采样栅格（`preview_scale`），所以"预览 100%"看到的是缩图观感，
    判断不了输出像素级的锐化。这个接口给出真正的 1:1 像素。
    """
    import numpy as np

    proj = STATE.require()
    with LOCK:
        key = (str(proj.dir), int(proj.doc.get("rev", 0)))
        if _FULL_CACHE.get("key") != key:
            rgb, alpha = proj.render_full(1.0)
            _FULL_CACHE.update(
                key=key,
                rgb=(np.clip(rgb, 0, 1) * 255 + 0.5).astype("uint8"),
                alpha=(np.clip(alpha, 0, 1) * 255 + 0.5).astype("uint8"),
            )
        rgb8, a8 = _FULL_CACHE["rgb"], _FULL_CACHE["alpha"]
        H, W = int(rgb8.shape[0]), int(rgb8.shape[1])
        x = max(0, min(int(x), max(0, W - 16)))
        y = max(0, min(int(y), max(0, H - 16)))
        w = max(16, min(int(w), W - x))
        h = max(16, min(int(h), H - y))
        sub = rgb8[y:y + h, x:x + w].astype("float32") / 255.0
        suba = a8[y:y + h, x:x + w].astype("float32") / 255.0
        pil = render.to_pil(render.flatten_on(sub, suba), None)
        bio = io.BytesIO()
        pil.save(bio, format="JPEG", quality=92, optimize=False, subsampling=0)
        out = bio.getvalue()
    return Response(out, media_type="image/jpeg", headers={
        "Cache-Control": "no-store, max-age=0",
        "X-Image-W": str(W), "X-Image-H": str(H),
        "X-View-X": str(x), "X-View-Y": str(y), "X-View-W": str(w), "X-View-H": str(h),
    })


@app.post("/api/project/close")
def api_project_close() -> dict:
    """关闭当前工程（不动磁盘上的数据）。

    为什么需要它：服务持有当前 Project 对象，只要它还"开着"，任何保存都会把目录写回来——
    验收脚本想清掉自己造的 e2e_* 工程，就必须先让服务松手。
    """
    with LOCK:
        was = str((STATE.project.doc.get("name") if STATE.project else "") or "")
        STATE.project = None
    log_event("project_close", name=was)
    return {"ok": True, "closed": was}


@app.post("/api/teaching")
def api_teaching(payload: dict = Body(...)) -> dict:
    proj = STATE.require()
    # 没传 target 时用设置里的选择（界面上的「看原图 / 看当前成图」开关就存在这里）
    default_target = str((STATE.settings.data.get("teaching") or {}).get("target") or "original")
    with LOCK:
        c = teach_mod.TeachingSession(proj, STATE.settings).critique(
            target=str(payload.get("target") or default_target),
            apply_edits=bool(payload.get("apply_edits")))
    log_event("teaching", model=c.model, scores=c.scores, target=c.target)
    return {"ok": True, "critique": c.to_dict(), "project": proj.state()}


# ---------------------------------------------------------------- 设置

@app.get("/api/settings")
def api_settings() -> dict:
    return {"ok": True, "settings": STATE.settings.public(), "presets": [
        p.to_dict(with_key=False) for p in preset_models()]}


@app.post("/api/settings")
def api_settings_save(payload: dict = Body(...)) -> dict:
    s = STATE.settings
    with LOCK:
        if "active_model" in payload:
            s.data["active_model"] = str(payload["active_model"] or "")
        if "teaching_model" in payload:
            s.data["teaching_model"] = str(payload["teaching_model"] or "")
        for key in ("preview", "matting", "export", "vision", "skills", "chat", "network",
                    "raw", "teaching"):
            if isinstance(payload.get(key), dict):
                s.data[key].update(payload[key])
        model = payload.get("model")
        if isinstance(model, dict) and model.get("id"):
            s.upsert_model(ModelProfile.from_dict(model))
        if payload.get("delete_model"):
            s.remove_model(str(payload["delete_model"]))
        if payload.get("reset_models"):
            s.data["models"] = [m.to_dict() for m in preset_models()]
        s.save()
        # 抠图引擎参数变了 → 释放已加载的模型，下次按新参数加载
        if "matting" in payload:
            for eng in list(matting._ENGINES.values()):
                eng.release()
    return {"ok": True, "settings": s.public()}


@app.post("/api/model/test")
def api_model_test(payload: dict | None = Body(default=None)) -> Any:
    mid = str((payload or {}).get("model") or "")
    prof = STATE.settings.model(mid or None)
    client = llm.LLMClient(prof, timeout=60)
    try:
        client.check_ready()
        res = client.chat([{"role": "user", "content": "只回复两个字：可用"}],
                          temperature=0, max_tokens=16)
        return {"ok": True, "model": prof.model, "name": prof.name,
                "sample": (res.text or "").strip()[:60], "elapsed": round(res.elapsed, 2),
                "vision": prof.vision}
    except llm.LLMError as e:
        return JSONResponse({"ok": False, "error": str(e), "model": prof.model,
                             "name": prof.name}, status_code=200)


@app.get("/api/matting/status")
def api_matting_status() -> dict:
    m = STATE.settings.matting
    eng = matting.get_engine(m.get("model_dir", ""), m.get("device", "auto"),
                             int(m.get("resolution", 1024)))
    return {"ok": True, "status": eng.status()}


@app.post("/api/matting/release")
def api_matting_release() -> dict:
    for eng in list(matting._ENGINES.values()):
        eng.release()
    return {"ok": True}


# ---------------------------------------------------------------- RAW（CR3 等）

@app.get("/api/raw/status")
def api_raw_status() -> dict:
    """报告本机每条 RAW 解码路径的可用性，以及 auto 模式会走哪条（推广到别的机器时先看这个）。"""
    mode = str(STATE.settings.raw.get("decode", "auto"))
    methods = raw_io.method_status()
    return {"ok": True,
            "rawpy_available": raw_io.rawpy_available(),
            "wic": raw_io.wic_status(),
            "decode_mode": mode,
            "auto_will_use": raw_io.resolve_method(mode),
            "methods": methods,
            "raw_extensions": sorted(e.lstrip(".").upper() for e in raw_io.RAW_EXTS),
            "note": ("auto 按 rawpy → WIC → 内嵌 JPEG 依次回退，因此任何机器上都至少有一条可用；"
                     "解码只影响导入那一刻，已建工程在不同机器上渲染完全一致。")}


@app.post("/api/raw/probe")
def api_raw_probe(payload: dict = Body(...)) -> dict:
    path = str(payload.get("path") or "")
    if not path:
        raise HTTPException(400, "缺少 path")
    return {"ok": True, "info": raw_io.probe(path)}


@app.get("/api/health")
def api_health() -> dict:
    return {"ok": True, "app": APP_NAME, "version": __version__,
            "has_project": STATE.project is not None,
            "project_dir": str(STATE.project.dir) if STATE.project else ""}


@app.post("/api/shutdown")
def api_shutdown() -> dict:
    """界面上的「退出」按钮：关掉本地服务进程。

    优先**优雅退出**（uvicorn 的 should_exit），这样本次响应一定先发出去；
    若优雅退出没生效（例如有请求长期占着事件循环），再由看门狗兜底强杀。
    旧实现直接 os._exit，响应与退出会抢跑，浏览器偶发看到连接被重置。
    """
    import os
    import threading as _t

    log_event("shutdown")
    srv = _UVICORN.get("server")

    def _bye() -> None:
        time.sleep(0.2)
        if srv is not None:
            try:
                srv.should_exit = True
            except Exception:
                pass
            for _ in range(60):                      # 最多等 3 秒优雅收尾
                time.sleep(0.05)
                if getattr(srv, "finished", False):
                    break
        os._exit(0)

    _t.Thread(target=_bye, daemon=True).start()
    return {"ok": True, "message": "正在退出…", "graceful": srv is not None}


def set_uvicorn_server(srv: object) -> None:
    """由 __main__ 注入 uvicorn 的 Server 实例，供 /api/shutdown 做优雅退出。"""
    _UVICORN["server"] = srv
