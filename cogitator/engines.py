"""外部重型引擎适配（可选）。

## 定位

本项目的强项是"AI 出指令 + 本地确定性算子"，但只有 48 个算子。像 PSD 图层、智能对象、
排版、矢量、GPU 合成这类重活，外面已有做得更深的工具（例如 photocraft：858 条引擎命令）。
这个模块让本项目能把**单条指令转发给外部引擎**，并把产物作为新图层收回当前工程。

## 三条设计纪律

1. **进程边界，不做 FFI。** 外部引擎是独立程序，通过命令行调用；它崩溃不会带走本服务，
   版本可各自独立升级，本仓库也不需要引入 Rust 工具链。
2. **默认关闭。** 外部引擎处于早期阶段，接口会变；因此必须显式启用（配置或环境变量），
   且所有失败都退化成可读错误，不影响本地算子与其余功能。
3. **不改变可重放性。** 外部引擎的执行**不写进图层管线**（它不是确定性算子，重放无意义）。
   结果以"新图层"的形式进入工程，与手动导入一张图完全同路——这样撤销、重放、导出
   仍然走原有那条已验证的路径。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

# 引擎定义：id → 探测用命令行与提示
ENGINES: dict[str, dict] = {
    "photocraft": {
        "label": "PhotoCraft",
        "env": "PHOTOCRAFT_CLI",
        "bin": "photocraft-cli",
        "homepage": "https://github.com/storytold/photocraft",
        "note": "Rust 写的 Photoshop 类编辑器（MIT OR Apache-2.0），自带无头 CLI 与 858 条引擎命令",
    },
}

_CACHE: dict[str, dict] = {"registry": None, "at": 0.0, "cli": ""}
REGISTRY_TTL = 600.0          # 命令注册表缓存 10 分钟（它有 260KB+，每次现查太浪费）


def engine_conf(settings, engine_id: str = "photocraft") -> dict:
    """取该引擎的配置（settings.external_engines.<id>）。"""
    try:
        conf = (settings.data.get("external_engines") or {}).get(engine_id) or {}
    except Exception:
        conf = {}
    return conf


def resolve_cli(settings, engine_id: str = "photocraft") -> str:
    """按 配置 → 环境变量 → PATH 的顺序找可执行文件，找不到返回空串。"""
    meta = ENGINES.get(engine_id)
    if not meta:
        return ""
    conf = engine_conf(settings, engine_id)
    for cand in (str(conf.get("cli") or "").strip(),
                 os.environ.get(meta["env"], "").strip()):
        if cand and Path(cand).is_file():
            return cand
    found = shutil.which(meta["bin"]) or shutil.which(meta["bin"] + ".exe")
    return found or ""


def _run_cli(cli: str, args: list[str], timeout: float) -> tuple[int, str, str]:
    p = subprocess.run([cli] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def registry(settings, engine_id: str = "photocraft") -> list[dict]:
    """取引擎命令注册表（带缓存）。取不到就返回空列表，不抛异常。"""
    cli = resolve_cli(settings, engine_id)
    if not cli:
        return []
    if (_CACHE["registry"] is not None and _CACHE["cli"] == cli
            and time.time() - _CACHE["at"] < REGISTRY_TTL):
        return _CACHE["registry"]
    try:
        code, out, _err = _run_cli(cli, ["commands", "--json"], 90)
        data = json.loads(out) if code == 0 and out.strip().startswith("[") else []
    except Exception:
        data = []
    _CACHE.update(registry=data, at=time.time(), cli=cli)
    return data


def status(settings) -> dict:
    """各引擎的可用性概览（给界面/MCP/诊断用）。"""
    out = []
    for eid, meta in ENGINES.items():
        conf = engine_conf(settings, eid)
        cli = resolve_cli(settings, eid)
        reg = registry(settings, eid) if cli else []
        out.append({
            "id": eid, "label": meta["label"], "homepage": meta["homepage"],
            "enabled": bool(conf.get("enabled")), "cli": cli, "available": bool(cli),
            "command_count": len(reg),
            "enabled_commands": len([c for c in reg if c.get("enabled")]),
            "note": (meta["note"] if cli else
                     f"未找到 {meta['bin']}。安装后设置环境变量 {meta['env']} "
                     f"指向可执行文件，或在设置里填 external_engines.{eid}.cli"),
        })
    return {"ok": True, "engines": out}


def commands(settings, engine_id: str = "photocraft", flt: str = "",
             prefix: str = "", limit: int = 0, enabled_only: bool = False) -> dict:
    reg = registry(settings, engine_id)
    items = reg
    if enabled_only:
        items = [c for c in items if c.get("enabled")]
    if prefix:
        items = [c for c in items if str(c.get("id", "")).startswith(prefix)]
    if flt:
        f = flt.lower()
        items = [c for c in items if f in json.dumps(c, ensure_ascii=False).lower()]
    total = len(items)
    if limit > 0:
        items = items[:limit]
    return {"ok": True, "engine": engine_id, "total": total, "returned": len(items),
            "commands": items}


def categories(reg: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in reg:
        cid = str(c.get("id") or "")
        if "." in cid:
            out[cid.split(".")[0]] = out.get(cid.split(".")[0], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def run(settings, engine_id: str, command: str, params: dict | None,
        timeout: float = 180.0, in_path: str = "", out_path: str = "",
        extra: list[str] | None = None) -> dict:
    """在给定输入文件上执行一条引擎命令，产物写到 out_path。

    调用形态（photocraft）：`photocraft-cli run <in> --cmd <id> [--params <json>] --out <out>`
    """
    meta = ENGINES.get(engine_id)
    if not meta:
        raise ValueError(f"未知引擎：{engine_id}（可用：{', '.join(ENGINES)}）")
    conf = engine_conf(settings, engine_id)
    if not conf.get("enabled"):
        raise ValueError(f"引擎 {engine_id} 未启用：请在设置里把 external_engines."
                         f"{engine_id}.enabled 设为 true（默认关闭，避免依赖早期项目）")
    cli = resolve_cli(settings, engine_id)
    if not cli:
        raise ValueError(f"找不到 {meta['bin']}：安装后设置环境变量 {meta['env']}，"
                         f"或在设置里填 external_engines.{engine_id}.cli")
    if not command:
        raise ValueError("必须给出引擎命令 id（可用 engine/commands 查询）")
    if not in_path or not Path(in_path).is_file():
        raise ValueError(f"输入文件不存在：{in_path}")
    out_path = out_path or str(Path(in_path).with_name(Path(in_path).stem + "-engine.jpg"))
    args = ["run", str(in_path), "--cmd", str(command)]
    if params:
        args += ["--params", json.dumps(params, ensure_ascii=False)]
    args += ["--out", str(out_path)]
    if extra:
        args += [str(x) for x in extra]
    t0 = time.time()
    code, out, err = _run_cli(cli, args, float(timeout))
    took = round(time.time() - t0, 2)
    produced = Path(out_path)
    return {"ok": code == 0 and produced.is_file(), "engine": engine_id,
            "command": command, "params": params or {}, "cli": cli,
            "output": str(produced) if produced.is_file() else "",
            "output_size": (produced.stat().st_size if produced.is_file() else 0),
            "seconds": took, "exit_code": code,
            "stdout": out[-2000:], "stderr": err[-2000:]}
