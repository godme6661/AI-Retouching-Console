"""风格配方：把审美固化成「意图 + 一串确定性算子」，避免每次都靠模型临场发挥。

配方本身不产生新算子：套用一个风格 = 把若干已登记算子按顺序追加到图层管线里，
因此每一步仍然独立可见、可微调、可单独删除。强度只缩放"强度类"参数
（见 ops.style_scalable），色相角度这类语义参数不缩放。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ops

STYLES_FILE = Path(__file__).resolve().parent / "styles.json"


@dataclass
class Style:
    id: str
    name: str
    group: str
    intent: str
    suits: list[str] = field(default_factory=list)
    caution: str = ""
    ops: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "group": self.group,
            "intent": self.intent, "suits": list(self.suits),
            "caution": self.caution, "op_count": len(self.ops),
            "ops": [{k: v for k, v in o.items()} for o in self.ops],
        }

    def summary(self) -> str:
        suits = "/".join(self.suits) if self.suits else "通用"
        line = f"- {self.id}（{self.name}｜{self.group}｜适合 {suits}）：{self.intent}"
        if self.caution:
            line += f" ⚠{self.caution}"
        return line


_CACHE: list[Style] | None = None


def load_styles(force: bool = False) -> list[Style]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    raw = json.loads(STYLES_FILE.read_text(encoding="utf-8"))
    out: list[Style] = []
    for item in raw.get("styles", []):
        out.append(Style(
            id=str(item["id"]), name=str(item.get("name") or item["id"]),
            group=str(item.get("group") or "通用"),
            intent=str(item.get("intent") or ""),
            suits=[str(s) for s in item.get("suits") or []],
            caution=str(item.get("caution") or ""),
            ops=list(item.get("ops") or []),
        ))
    _CACHE = out
    return out


def get_style(key: str) -> Style:
    key = (key or "").strip()
    for s in load_styles():
        if key in (s.id, s.name):
            return s
    raise KeyError(key)


def style_ids() -> list[str]:
    return [s.id for s in load_styles()]


# 中性值不等于 0 的参数：按"向中性值插值"缩放，否则会出现 gamma=0 这类非法值
NEUTRAL_VALUES: dict[str, float] = {"gamma": 1.0}
# 形状/绝对点类参数：完全不随强度缩放（缩放会把过渡与颗粒尺寸也压没）
NON_SCALABLE = {"feather", "size", "radius", "width"}


def scale_ops(style_ops: list[dict], strength: float = 100.0) -> list[dict]:
    """按 strength(0~150) 缩放强度类参数；0% 直接返回空列表（真正的不动手）。"""
    k = max(0.0, min(150.0, float(strength))) / 100.0
    if k <= 0.0:
        return []
    out: list[dict] = []
    for op in style_ops:
        new = {"op": op["op"]}
        for key, val in op.items():
            if key == "op":
                continue
            if (isinstance(val, (int, float)) and not isinstance(val, bool)
                    and key not in NON_SCALABLE and ops.style_scalable(key)):
                base = NEUTRAL_VALUES.get(key, 0.0)
                scaled = base + (val - base) * k
                new[key] = int(round(scaled)) if isinstance(val, int) else round(scaled, 4)
            else:
                new[key] = val
        out.append(new)
    return out


def expand(key: str, strength: float = 100.0) -> tuple[Style, list[dict]]:
    """返回 (风格, 已缩放且通过注册表校验的算子列表)。校验失败会抛 ops.OpError。"""
    style = get_style(key)
    scaled = scale_ops(style.ops, strength)
    validated = ops.validate_ops(scaled, max_ops=60)
    return style, validated


def manual() -> str:
    """给模型看的风格清单（不含具体算子，避免它照抄数值而失去判断）。"""
    lines = ["【可选风格配方】用 apply_style 套用，参数 style=配方 id、strength=强度（0~150，100 为原配方，0 表示不动手）"]
    for s in load_styles():
        lines.append(s.summary())
    lines.append("风格是起点不是终点：套用后仍应针对这张照片微调至少一处"
                 "（例如肤色偏橙就压低 highlight_sat，高光过曝就再补一点 highlights 负值）。"
                 "不要连续套两个风格，会得到脏灰。")
    return "\n".join(lines)


def self_check() -> list[str]:
    """校验每个配方的每个算子都能通过注册表（自检用）。"""
    errors: list[str] = []
    seen: set[str] = set()
    for s in load_styles():
        if s.id in seen:
            errors.append(f"{s.id}: id 重复")
        seen.add(s.id)
        if not s.ops:
            errors.append(f"{s.id}: 没有任何算子")
        for i, op in enumerate(s.ops):
            spec = ops.SPEC_BY_NAME.get(op.get("op", ""))
            if spec is None:
                errors.append(f"{s.id} 第{i + 1}条: 未登记的操作 {op.get('op')!r}")
                continue
            if spec.kind != "pipeline":
                errors.append(f"{s.id} 第{i + 1}条: {op['op']} 不是管线算子（配方只能用管线算子）")
            try:
                ops.validate_op(op, i)
            except ops.OpError as e:
                errors.append(f"{s.id} 第{i + 1}条: {e}")
        # 强度 0 / 50 / 150 三档都必须能通过校验
        for k in (0, 50, 150):
            try:
                ops.validate_ops(scale_ops(s.ops, k), max_ops=60)
            except ops.OpError as e:
                errors.append(f"{s.id} 强度{k}%: {e}")
    return errors
