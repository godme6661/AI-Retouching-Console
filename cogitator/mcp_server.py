"""机魂修图台的 MCP 服务器（stdio）——让任何 MCP 客户端都能驱动这个修图台。

## 为什么这样做

项目的构想说"让 AI 去操控图像编辑工具"。这个模块把这句话反过来实现一次：
**让这个修图台本身成为可被 AI 操控的工具**——任何支持 MCP 的客户端（Claude、IDE 助手、
其他 agent 框架）接上它，就能发现并执行这里的算子、看文档状态、取预览、导出成品。

## 三个设计取舍

1. **只做"桥"，不导入应用本体。** stdout 是 MCP 协议通道，任何一行日志/警告混进去都会
   破坏协议；因此本进程只用标准库 + HTTP 调那个正在运行的服务，不 import cogitator.*。
   代价是它需要服务已在运行（与 photocraft 的 `--bridge` 同一个思路）。
2. **单写者。** 所有改动仍由应用那一个进程落盘；MCP 侧不发并发写，避免两套状态打架。
3. **不做第二份契约。** 命令清单直接复用 `/api/ops/manual` 里的结构化 `specs`
   （48 个算子 + 13 套风格），与服务端校验器、界面微调滑杆同源。

## 用法

    # 先启动应用（另一个终端）
    python -m cogitator --port 8760

    # 再启动 MCP 服务器（客户端通过 stdio 与它通信）
    python -m cogitator.mcp_server --base http://127.0.0.1:8760

客户端配置示例（Claude Desktop / 其他 MCP 客户端）：

    { "mcpServers": { "cogitator": {
        "command": "python", "args": ["-m", "cogitator.mcp_server", "--base", "http://127.0.0.1:8760"] } } }
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import urllib.error
import urllib.request

PROTOCOL_FALLBACK = "2025-06-18"
SERVER_NAME = "cogitator"
SERVER_TITLE = "机魂修图台 (AI Retouching Console)"

BASE = "http://127.0.0.1:8760"


# ---------------------------------------------------------------- HTTP 桥

def _http(method: str, path: str, body: dict | None = None, timeout: float = 300.0):
    """调应用本体。返回 (status, bytes)。非 2xx 不抛异常，交给调用方解释。"""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:                                  # 服务没起来 / 连接被拒
        raise RuntimeError(f"无法连接机魂修图台服务（{BASE}）：{type(e).__name__}。"
                           f"请先在另一个终端运行 python -m cogitator --port 8760") from e


def _json(method: str, path: str, body: dict | None = None, timeout: float = 300.0) -> dict:
    status, raw = _http(method, path, body, timeout)
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:
        j = {"raw": raw[:400].decode("utf-8", "ignore")}
    if status >= 400:
        msg = j.get("detail") or j.get("error") or j.get("raw") or f"HTTP {status}"
        raise RuntimeError(f"{path} 出错（HTTP {status}）：{msg}")
    return j


def _base_url() -> str:
    return BASE


# ---------------------------------------------------------------- 工具实现

def t_command_list(args: dict) -> dict:
    man = _json("GET", "/api/ops/manual")
    flt = str(args.get("filter") or "").strip().lower()
    enabled_only = bool(args.get("enabled_only"))
    ops = []
    for s in man.get("specs", []):
        if flt and flt not in json.dumps(s, ensure_ascii=False).lower():
            continue
        ops.append({
            "id": s["name"], "kind": s["kind"], "category": s["category"],
            "summary": s["desc"], "needs_layer": s["layer"],
            "params": [{"name": p["name"], "kind": p["kind"], "desc": p["desc"],
                        "min": p.get("min"), "max": p.get("max"), "enum": p.get("enum"),
                        "required": p["required"]}
                       for p in s.get("params", [])],
        })
    styles = [{"id": s.get("id"), "name": s.get("name"), "desc": s.get("desc"),
               "strength": s.get("strength")} for s in man.get("style_list", [])]
    out = {"ok": True, "count": len(ops), "commands": ops, "styles": styles,
           "notes": ("每个命令的参数区间由服务端校验，越界会返回 HTTP 400（整批不生效）。"
                     "批量执行是本项目的原子语义：整批成功或整批失败，且只占一次撤销。")}
    if not enabled_only:
        out["manual"] = man.get("manual", "")
    return out


def _run_ops(ops: list) -> dict:
    r = _json("POST", "/api/ops", {"ops": ops})
    p = r.get("project") or {}
    return {"ok": True, "applied": len(ops), "rev": p.get("rev"),
            "layers": [{"id": l["id"], "ops": len(l.get("ops") or [])} for l in p.get("layers", [])],
            "undo_depth": p.get("undo_depth"), "redo_depth": p.get("redo_depth")}


def t_command_run(args: dict) -> dict:
    if isinstance(args.get("ops"), list) and args["ops"]:
        return _run_ops(args["ops"])
    if args.get("id"):
        op = {"op": args["id"]}
        for k, v in (args.get("params") or {}).items():
            op[k] = v
        if args.get("layer"):
            op["layer"] = args["layer"]
        return _run_ops([op])
    raise ValueError("需要提供 ops（数组）或 id + params")


def t_command_batch(args: dict) -> dict:
    steps = args.get("steps") or []
    if not steps:
        raise ValueError("steps 不能为空")
    return _run_ops(steps)


def t_doc_inspect(_args: dict) -> dict:
    st = _json("GET", "/api/state")
    p = st.get("project")
    if not p:
        return {"ok": True, "document": None,
                "recent": [{"name": r.get("name"), "dir": r.get("dir")} for r in st.get("recent", [])],
                "note": "当前没有打开的工程；可用 doc_open 打开图片或已有工程。"}
    return {"ok": True, "document": {
        "name": p.get("name"), "rev": p.get("rev"),
        "canvas": p.get("canvas"), "edited": p.get("edited"),
        "assets": [{"id": a["id"], "name": a.get("name"), "size": a.get("size"),
                    "source_kind": a.get("source_kind")} for a in p.get("assets", [])],
        "layers": [{"id": l["id"], "name": l.get("name"), "visible": l.get("visible"),
                    "opacity": l.get("opacity"), "blend": l.get("blend"),
                    "x": l.get("x"), "y": l.get("y"), "scale": l.get("scale"),
                    "ops": l.get("ops")} for l in p.get("layers", [])],
        "active_layer": p.get("active"), "preview_scale": p.get("preview_scale"),
    }, "undo_depth": p.get("undo_depth"), "redo_depth": p.get("redo_depth")}


def t_doc_open(args: dict) -> dict:
    paths = args.get("paths") or []
    if not paths:
        raise ValueError("需要提供 paths（图片或工程目录的绝对路径数组）")
    r = _json("POST", "/api/open", {"paths": paths, "mode": args.get("mode") or "new",
                                    "name": args.get("name") or ""})
    return {"ok": True, "project": (r.get("project") or {}).get("name"),
            "canvas": (r.get("project") or {}).get("canvas")}


def t_doc_close(_args: dict) -> dict:
    return {"ok": True, "closed": _json("POST", "/api/project/close", {}).get("ok", True)}


def t_undo(_args: dict) -> dict:
    r = _json("POST", "/api/undo", {})
    return {"ok": True, "undo_depth": (r.get("project") or {}).get("undo_depth"),
            "redo_depth": (r.get("project") or {}).get("redo_depth")}


def t_redo(_args: dict) -> dict:
    r = _json("POST", "/api/redo", {})
    return {"ok": True, "undo_depth": (r.get("project") or {}).get("undo_depth"),
            "redo_depth": (r.get("project") or {}).get("redo_depth")}


def _image_tool(path: str, args: dict, mime: str = "image/jpeg") -> dict:
    status, raw = _http("GET", path, timeout=300)
    if status >= 400:
        raise RuntimeError(f"{path} 渲染失败（HTTP {status}）")
    max_side = int(args.get("max_side") or 0)
    if max_side > 0:
        try:                                                # 只做等比缩放，不改内容
            from PIL import Image
            im = Image.open(io.BytesIO(raw))
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=88)
                raw = buf.getvalue()
        except Exception:
            pass                                            # 缩放失败就用原图，不报错
    return {"ok": True,
            "content": [{"type": "image", "data": base64.b64encode(raw).decode("ascii"),
                         "mimeType": mime}]}


def t_render_preview(args: dict) -> dict:
    return _image_tool("/project/preview", args)


def t_render_viewport(args: dict) -> dict:
    """按输出像素取一块（真 1:1），用于判断锐化与噪点。"""
    q = {k: int(args.get(k) or d) for k, d in
         (("x", 0), ("y", 0), ("w", 900), ("h", 700))}
    st = _json("GET", "/api/state")
    p = st.get("project")
    if not p:
        raise RuntimeError("当前没有打开的工程")
    q["w"] = min(q["w"], int(p["canvas"]["w"]))
    q["h"] = min(q["h"], int(p["canvas"]["h"]))
    path = f"/project/view?x={q['x']}&y={q['y']}&w={q['w']}&h={q['h']}&t={p.get('rev')}"
    return _image_tool(path, args)


def t_export_image(args: dict) -> dict:
    body = {"format": args.get("format") or "jpg",
            "quality": int(args.get("quality") or 92),
            "max_side": int(args.get("max_side") or 0),
            "path": args.get("path") or ""}
    r = _json("POST", "/api/export", body)
    return {"ok": True, "path": r.get("path"), "size": r.get("size")}


def t_engine_status(_args: dict) -> dict:
    """外部重型引擎（如 photocraft）是否可用——未启用时给出可读提示。"""
    try:
        return _json("GET", "/api/engine/status")
    except RuntimeError as e:
        return {"ok": True, "engines": [],
                "note": f"本版本没有外部引擎接口（{e}）"}


def t_engine_run(args: dict) -> dict:
    return _json("POST", "/api/engine/run", {
        "engine": args.get("engine") or "photocraft",
        "command": args.get("command"),
        "params": args.get("params") or {},
        "timeout": int(args.get("timeout") or 180),
    }, timeout=float(args.get("timeout") or 180) + 60)


# ---------------------------------------------------------------- 工具清单

TOOLS: list[tuple[str, str, dict, callable]] = [
    ("command_list", "列出本修图台的全部算子与风格配方（含参数区间与说明）。参数 filter 可按关键词过滤。",
     {"type": "object", "properties": {
         "filter": {"type": "string", "description": "关键词，如 曝光 / 抠图 / 几何"},
         "enabled_only": {"type": "boolean", "description": "true 时不返回完整手册文本"}}},
     t_command_list),
    ("command_run", "执行一条或多条算子（非破坏性管线；源图不改）。可传 ops 数组，或 id + params。",
     {"type": "object", "properties": {
         "ops": {"type": "array", "items": {"type": "object"},
                 "description": '算子数组，如 [{"op":"exposure","value":0.3,"layer":"L1"}]'},
         "id": {"type": "string", "description": "单个算子名，如 exposure"},
         "params": {"type": "object", "description": "配合 id 使用"},
         "layer": {"type": "string", "description": "目标图层，默认活动图层"}}},
     t_command_run),
    ("command_batch", "按顺序执行一批算子。本项目的批量是原子语义：整批成功或整批失败，只占一次撤销。",
     {"type": "object", "properties": {
         "steps": {"type": "array", "items": {"type": "object"}},
         "stop_on_error": {"type": "boolean", "description": "保留参数；本项目本就整批原子"}},
      "required": ["steps"]},
     t_command_batch),
    ("doc_inspect", "查看当前文档：画布、图层树、每个图层的算子管线、撤销/重做深度；未打开工程时列出最近工程。",
     {"type": "object", "properties": {}}, t_doc_inspect),
    ("doc_open", "打开图片新建工程，或打开已有工程目录（mode=project）。",
     {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}},
         "mode": {"type": "string", "enum": ["new", "project"]},
         "name": {"type": "string"}}, "required": ["paths"]},
     t_doc_open),
    ("doc_close", "关闭当前工程（不动磁盘上的数据）。",
     {"type": "object", "properties": {}}, t_doc_close),
    ("render_preview", "取当前成品的预览图（下采样栅格，最快）。返回图像内容块。",
     {"type": "object", "properties": {"max_side": {"type": "integer", "description": "可选，等比缩到这个最长边"}}},
     t_render_preview),
    ("render_viewport", "按输出像素取一块真 1:1 视图（判断锐化与噪点用），可指定 x/y/w/h。",
     {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "w": {"type": "integer"}, "h": {"type": "integer"}}},
     t_render_viewport),
    ("undo", "撤销上一批修改（快照式，最多 60 步）。",
     {"type": "object", "properties": {}}, t_undo),
    ("redo", "重做。", {"type": "object", "properties": {}}, t_redo),
    ("export_image", "导出成品到磁盘（JPG/PNG/WEBP，可限制最长边）。",
     {"type": "object", "properties": {
         "path": {"type": "string", "description": "留空则导出到工程的 exports/"},
         "format": {"type": "string", "enum": ["jpg", "png", "webp"]},
         "quality": {"type": "integer"}, "max_side": {"type": "integer"}}},
     t_export_image),
    ("engine_status", "查询可选的外部重型引擎（如 photocraft 的 858 条命令）是否可用。",
     {"type": "object", "properties": {}}, t_engine_status),
    ("engine_run", "把一条命令交给外部重型引擎执行（需要已配置并启用该引擎）。",
     {"type": "object", "properties": {
         "engine": {"type": "string"}, "command": {"type": "string"},
         "params": {"type": "object"}, "timeout": {"type": "integer"}},
      "required": ["command"]},
     t_engine_run),
]


# ---------------------------------------------------------------- JSON-RPC

def _tools_payload() -> list[dict]:
    inv = {n for n, *_ in TOOLS}
    out = []
    for name, desc, schema, _fn in TOOLS:
        if name.startswith("engine_") and name not in inv:
            continue
        out.append({"name": name, "title": desc.split("。")[0], "description": desc,
                    "inputSchema": schema,
                    "annotations": {"readOnlyHint": name in (
                        "command_list", "doc_inspect", "render_preview", "render_viewport",
                        "engine_status"),
                        "destructiveHint": name in ("doc_close", "export_image"),
                        "idempotentHint": name in ("command_list", "doc_inspect", "render_preview",
                                                   "render_viewport", "engine_status"),
                        "openWorldHint": name in ("engine_run",)}})
    return out


def _tool_result(payload: dict) -> dict:
    if isinstance(payload.get("content"), list):
        return {"content": payload["content"], "isError": False}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > 200_000:
        text = text[:200_000] + "\n…（已截断）"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if mid is None and method and method.startswith("notifications/"):
        return None                                          # 通知不需要回包
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if isinstance(want, str) and want else PROTOCOL_FALLBACK,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"subscribe": False, "listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": _version()},
            "instructions": ("这是本机的 AI 修图台：可用 command_list 发现 48 个算子，"
                             "command_run/command_batch 执行（非破坏性、可撤销、整批原子），"
                             "doc_inspect 看文档状态，render_preview/render_viewport 看图，"
                             "export_image 出片。修改像素的只有这些确定性算子，模型不直接改像素。")}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tools_payload()}}
    if method == "tools/call":
        name = params.get("name")
        fn = next((f for n, _d, _s, f in TOOLS if n == name), None)
        if fn is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"未知工具：{name}"}}
        try:
            return {"jsonrpc": "2.0", "id": mid, "result": _tool_result(fn(params.get("arguments") or {}))}
        except Exception as e:                               # 工具失败走 isError，不是协议错误
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                               "isError": True}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": [
            {"uri": "cogitator://commands", "name": "算子清单", "mimeType": "application/json",
             "description": "48 个算子与 13 套风格配方的参数契约"},
            {"uri": "cogitator://document", "name": "当前文档", "mimeType": "application/json",
             "description": "画布、图层树、算子管线与撤销深度"}]}}
    if method == "resources/read":
        uri = params.get("uri")
        try:
            if uri == "cogitator://commands":
                data = t_command_list({"enabled_only": True})
            elif uri == "cogitator://document":
                data = t_doc_inspect({})
            else:
                return {"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32602, "message": f"未知资源：{uri}"}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"contents": [
            {"uri": uri, "mimeType": "application/json",
             "text": json.dumps(data, ensure_ascii=False, indent=2)}]}}
    if method in ("completion/complete", "logging/setLevel", "prompts/list"):
        key = "prompts" if method == "prompts/list" else None
        if key:
            return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未实现的方法：{method}"}}


def _version() -> str:
    try:
        import pathlib
        txt = pathlib.Path(__file__).with_name("__init__.py").read_text(encoding="utf-8")
        for ln in txt.split("\n"):
            if ln.startswith("__version__"):
                return ln.split("=")[1].strip().strip('"')
    except Exception:
        pass
    return "0"


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description="机魂修图台 MCP 服务器（stdio）")
    ap.add_argument("--base", default=BASE, help="应用本体的地址，默认 http://127.0.0.1:8760")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            resp = handle(msg)
        except json.JSONDecodeError as e:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"JSON 解析失败：{e}"}}
        except Exception as e:                               # 绝不让异常打断协议循环
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
