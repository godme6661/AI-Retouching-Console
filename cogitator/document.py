"""工程与文档模型：图层、算子管线、撤销重做、抠图物化、导出。

两条不变量：
  1. 源图永不被改写。所有效果都记录成算子管线，可随时重放 → 撤销/重做/逐步回退都成立。
  2. 结构算子立即改文档，管线/蒙版算子追加到图层管线。校验先于执行，任何一条非法
     整批不落地（避免留下半截状态）。
"""

from __future__ import annotations

import copy
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import image_ops as iop
from . import matting, ops, raw_io, render
from .paths import PROJECTS_DIR, safe_name, unique_dir

HISTORY_LIMIT = 60
# 支持普通图片与 RAW（CR3/CR2/NEF/ARW/DNG…，见 raw_io）
ASSET_EXT = raw_io.ALL_EXTS

# 几何算子：对比基准要保留它们（否则画幅对不齐），调色与蒙版则要丢掉
GEOMETRY_ONLY = {"crop", "crop_ratio", "rotate", "straighten", "flip", "resize"}
# 会改变"内容框"的几何算子：做完之后画布应当跟着适配，否则导出会留边
CROP_LIKE_OPS = {"crop", "crop_ratio", "straighten"}


class DocError(RuntimeError):
    """文档层错误（会被回灌给模型）。"""


@dataclass
class Project:
    dir: Path
    doc: dict = field(default_factory=dict)
    undo: list[dict] = field(default_factory=list)
    redo: list[dict] = field(default_factory=list)
    chat: list[dict] = field(default_factory=list)
    last_export: str = ""
    settings: Any = None
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ 存取
    @property
    def doc_file(self) -> Path:
        return self.dir / "document.json"

    @property
    def sources_dir(self) -> Path:
        return self.dir / "sources"

    @property
    def masks_dir(self) -> Path:
        return self.dir / "masks"

    @property
    def preview_dir(self) -> Path:
        return self.dir / "previews"

    @property
    def exports_dir(self) -> Path:
        return self.dir / "exports"

    def save(self) -> None:
        for d in (self.sources_dir, self.masks_dir, self.preview_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.doc_file.write_text(json.dumps(self.doc, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.dir / "chat.json").write_text(json.dumps(self.chat[-200:], ensure_ascii=False, indent=1),
                                            encoding="utf-8")

    @staticmethod
    def load(path: Path, settings: Any = None) -> "Project":
        path = Path(path)
        doc = json.loads((path / "document.json").read_text(encoding="utf-8"))
        chat_file = path / "chat.json"
        chat = json.loads(chat_file.read_text(encoding="utf-8")) if chat_file.is_file() else []
        p = Project(dir=path, doc=doc, chat=chat, settings=settings)
        return p

    # ------------------------------------------------------------ 创建
    @staticmethod
    def create_from_images(paths: list[str], settings: Any = None, name: str = "") -> "Project":
        valid = [Path(p) for p in paths if Path(p).is_file() and Path(p).suffix.lower() in ASSET_EXT]
        if not valid:
            raise DocError("没有可用图片（支持 png/jpg/webp/bmp/tif/gif，以及 CR3/CR2/NEF/ARW/DNG 等 RAW）")
        pname = safe_name(name or valid[0].stem)
        pdir = unique_dir(PROJECTS_DIR, pname)
        pdir.mkdir(parents=True, exist_ok=True)
        for d in ("sources", "masks", "previews", "exports"):
            (pdir / d).mkdir(exist_ok=True)

        proj = Project(dir=pdir, settings=settings)
        proj.doc = {
            "name": pname,
            "created": datetime.now().isoformat(timespec="seconds"),
            "canvas": {"w": 100, "h": 100, "bg": "#ffffff"},
            "assets": [],
            "layers": [],
            "active": "",
            "rev": 0,
        }

        base_id = ""
        for i, src in enumerate(valid):
            asset = proj.import_asset(str(src))
            if i == 0:
                w, h = asset["size"]
                proj.doc["canvas"] = {"w": w, "h": h, "bg": None}
                layer = proj._new_layer(asset, x=w / 2.0, y=h / 2.0, name=src.stem)
                base_id = layer["id"]
            # 其余图片先进素材库，由用户/模型决定何时加入
        if base_id:
            proj.doc["active"] = base_id
        proj.save()
        proj._push_preview()
        return proj

    # ------------------------------------------------------------ 素材
    def import_asset(self, src_path: str) -> dict:
        """把外部图片（含 CR3 等 RAW）导入为工程源图。

        RAW 不会被整份复制（CR3 动辄 27MB）：走内嵌预览时直接写出其中的原始 JPEG 字节
        （零重编码），走 rawpy 时存 JPEG q95；原始路径与所用解码方式都记进素材元信息。
        """
        src = Path(src_path)
        if not src.is_file():
            raise DocError(f"文件不存在：{src_path}")
        if src.suffix.lower() not in ASSET_EXT:
            raise DocError(f"不支持的格式：{src.suffix}（支持图片与 CR3/CR2/NEF/ARW/DNG 等 RAW）")
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        n = len(self.doc["assets"]) + 1
        aid = f"A{n}"
        while any(self.sources_dir.glob(f"{aid}.*")) or any(a["id"] == aid for a in self.doc["assets"]):
            n += 1
            aid = f"A{n}"
        prefer = "auto"
        if self.settings is not None:
            prefer = str((self.settings.data.get("raw") or {}).get("decode") or "auto")
        try:
            dest, meta = raw_io.convert_to_source(src, self.sources_dir, aid, prefer=prefer)
        except raw_io.RawUnsupported as e:
            raise DocError(str(e)) from None
        w, h = meta.get("size") or (0, 0)
        asset = {"id": aid, "name": src.name, "file": f"sources/{dest.name}",
                 "size": [int(w), int(h)]}
        for k in ("source_kind", "decoded", "stored", "note", "original", "raw_size"):
            if meta.get(k):
                asset[k] = meta[k]
        self.doc["assets"].append(asset)
        return asset

    def asset(self, asset_id: str) -> dict:
        for a in self.doc["assets"]:
            if a["id"] == asset_id:
                return a
        raise DocError(f"没有素材 {asset_id!r}；现有素材：{self.asset_ids() or '无'}")

    def asset_ids(self) -> list[str]:
        return [a["id"] for a in self.doc["assets"]]

    # ------------------------------------------------------------ 图层
    def _next_layer_id(self) -> str:
        n = 1
        used = {l["id"] for l in self.doc["layers"]}
        while f"L{n}" in used:
            n += 1
        return f"L{n}"

    def _new_layer(self, asset: dict, x: float, y: float, name: str = "",
                   scale: float = 1.0, opacity: float = 1.0, blend: str = "normal") -> dict:
        lid = self._next_layer_id()
        layer = {
            "id": lid,
            "name": name or asset.get("name", lid),
            "source": asset["file"],
            "src_size": list(asset["size"]),
            "x": float(x),
            "y": float(y),
            "scale": float(scale),
            "rotation": 0.0,
            "flip_h": False,
            "flip_v": False,
            "opacity": float(opacity),
            "blend": blend,
            "visible": True,
            "ops": [],
        }
        self.doc["layers"].append(layer)
        return layer

    def layer(self, layer_id: str | None) -> dict:
        layers = self.doc["layers"]
        if not layers:
            raise DocError("文档里还没有图层")
        if layer_id in (None, "", "current", "active"):
            cand = self.doc.get("active") or layers[-1]["id"]
            for l in layers:
                if l["id"] == cand:
                    return l
            return layers[-1]
        for l in layers:
            if l["id"] == layer_id:
                return l
        for l in layers:
            if l["name"] == layer_id:
                return l
        raise DocError(f"没有图层 {layer_id!r}；现有图层：{[l['id'] for l in layers]}")

    def layer_index(self, layer_id: str) -> int:
        for i, l in enumerate(self.doc["layers"]):
            if l["id"] == layer_id:
                return i
        raise DocError(f"没有图层 {layer_id!r}")

    def set_active(self, layer_id: str) -> None:
        self.layer(layer_id)
        self.doc["active"] = layer_id
        self.save()

    # ------------------------------------------------------------ 撤销
    def _snapshot(self) -> None:
        self.undo.append(copy.deepcopy(self.doc))
        if len(self.undo) > HISTORY_LIMIT:
            self.undo.pop(0)
        self.redo.clear()

    def undo_step(self) -> bool:
        if not self.undo:
            return False
        self.redo.append(copy.deepcopy(self.doc))
        self.doc = self.undo.pop()
        self.doc["rev"] = int(self.doc.get("rev", 0)) + 1
        self.save()
        self._push_preview()
        return True

    def redo_step(self) -> bool:
        if not self.redo:
            return False
        self.undo.append(copy.deepcopy(self.doc))
        self.doc = self.redo.pop()
        self.doc["rev"] = int(self.doc.get("rev", 0)) + 1
        self.save()
        self._push_preview()
        return True

    # ------------------------------------------------------------ 单步微调
    def update_op(self, layer_id: str, index: int, params: dict) -> dict:
        """改写某一步已应用算子的参数（界面上拖滑杆用），保留原步骤位置。"""
        layer = self.layer(layer_id)
        ops_list = layer.get("ops") or []
        idx = int(index)
        if idx < 0:
            idx += len(ops_list)
        if not (0 <= idx < len(ops_list)):
            raise DocError(f"步骤序号越界：图层[{layer['id']}] 有 {len(ops_list)} 步（从 0 计）")
        cur = ops_list[idx]
        name = cur.get("op")
        allowed_here = {"feather", "grow", "invert"} if name in (
            "remove_bg", "mask_brush", "mask_from_color") else None
        merged = dict(cur)
        for k, v in (params or {}).items():
            if k in ("op", "mask_file", "mask_size", "layer"):
                continue
            if allowed_here is not None and k not in allowed_here:
                raise DocError(f"步骤 {name} 只支持调整 {sorted(allowed_here)}，不能改 {k}")
            merged[k] = v
        validated = ops.validate_op(merged)
        # 蒙版类步骤的蒙版文件不能被校验器丢掉
        for k in ("mask_file", "mask_size"):
            if k in cur:
                validated[k] = cur[k]
        self._snapshot()
        ops_list[idx] = validated
        self.doc["rev"] = int(self.doc.get("rev", 0)) + 1
        self.save()
        self._push_preview()
        return validated

    # ------------------------------------------------------------ 应用算子
    def apply_ops(self, raw_ops: Any) -> list[dict]:
        """校验 → 快照 → 执行。任何一条非法则整批不落地。"""
        validated = ops.validate_ops(raw_ops)          # 抛 OpError
        prepared: list[tuple[dict, dict]] = []          # (spec, op)
        for op in validated:
            spec = ops.SPEC_BY_NAME[op["op"]]
            prepared.append((spec, op))

        # 结构算子先做一次"目标是否存在"的预检，避免执行到一半失败
        for spec, op in prepared:
            if spec.kind == "structure" and spec.name in (
                "duplicate_layer", "delete_layer", "reorder_layer", "set_layer",
                "transform_layer", "align_layer", "fit_layer", "remove_op",
            ):
                if not op.get("layer"):
                    op["layer"] = self.layer(None)["id"]
                self.layer(op["layer"])                 # 不存在会抛 DocError
            if spec.kind in ("pipeline", "mask") and spec.layer:
                if not op.get("layer"):
                    op["layer"] = self.layer(None)["id"]
                self.layer(op["layer"])

        self._snapshot()
        results: list[dict] = []
        try:
            for spec, op in prepared:
                note = self._apply_one(spec, op)
                results.append({"op": op["op"], "ok": True, "note": note or ""})
            auto_note = self._maybe_fit_canvas_after_crop(prepared)
            if auto_note:
                results.append({"op": "canvas", "ok": True, "note": auto_note, "auto": True})
        except Exception:
            # 回滚到快照，保证文档状态与失败前一致
            if self.undo:
                self.doc = self.undo.pop()
            raise
        self.doc["rev"] = int(self.doc.get("rev", 0)) + 1
        self.save()
        self._push_preview()
        return results

    def _maybe_fit_canvas_after_crop(self, prepared: list) -> str:
        """裁切/校正是"图层级"操作，不会动作画布 —— 不管的话导出就会留下白边
        （实测：1200×900 做 1:1 裁切后导出仍是 1200×900，左右各一条白边）。

        这里在**保守条件**下自动把画布适配到内容，且与整批共用同一个快照，
        因此一次撤销可以把"裁切 + 画布适配"一起回退：
          * 本批确实做了裁切/校正类操作；
          * 文档只有一个图层（多图层合成时用户可能故意保留大画布，不自动改）；
          * 用户本批没有显式给 canvas / canvas_ratio 指令（尊重其明确意图）；
          * 画布与内容框确实不一致（否则不必动）。
        """
        names = {op["op"] for _spec, op in prepared}
        if not (names & CROP_LIKE_OPS) or (names & {"canvas", "canvas_ratio"}):
            return ""
        if len(self.doc.get("layers", [])) != 1:
            return ""
        if self.doc["canvas"].get("locked"):
            return ""                      # 用户手动设定过画布 → 不再自作主张
        lw, lh = self._content_bounds()
        nw, nh = max(16, int(round(lw))), max(16, int(round(lh)))
        cw, ch = int(self.doc["canvas"]["w"]), int(self.doc["canvas"]["h"])
        if (nw, nh) == (cw, ch):
            return ""
        self._set_canvas(nw, nh, "fit")
        return f"画布已随裁切自动适配内容 → {nw}x{nh}（导出不再留边）"

    def _apply_one(self, spec: ops.OpSpec, op: dict) -> str:
        if spec.kind == "pipeline":
            layer = self.layer(op.get("layer"))
            rec = {k: v for k, v in op.items() if k != "layer"}
            layer["ops"].append(rec)
            return f"{op['op']} → 图层[{layer['id']}] 第 {len(layer['ops'])} 步"
        if spec.kind == "mask":
            return self._apply_mask(spec, op)
        return self._apply_structure(spec, op)

    # ---- 蒙版：先在当前管线上算出蒙版并落盘成 PNG，再作为管线步骤记录 ----
    def _apply_mask(self, spec: ops.OpSpec, op: dict) -> str:
        layer = self.layer(op.get("layer"))
        rgb, alpha = render.render_layer_raster(layer, self.dir)
        H, W = rgb.shape[:2]

        if op["op"] == "clear_mask":
            layer["ops"].append({"op": "clear_mask"})
            return f"已清空图层[{layer['id']}]的蒙版"

        if op["op"] == "remove_bg":
            pil = render.to_pil(rgb)
            m, engine = matting.remove_background(
                pil,
                model_dir=(self.settings.matting.get("model_dir") if self.settings else "") or "",
                engine=op.get("engine", "auto") or "auto",
                device=(self.settings.matting.get("device", "auto") if self.settings else "auto"),
                resolution=int(self.settings.matting.get("resolution", 1024) if self.settings else 1024),
            )
            if m.shape[:2] != (H, W):
                from PIL import Image

                m = np.asarray(Image.fromarray((m * 255).astype(np.uint8), mode="L").resize((W, H)), np.float32) / 255.0
            note = f"抠图引擎={engine}，主体占比约 {float((m > 0.5).mean()) * 100:.1f}%"
        elif op["op"] == "mask_from_color":
            base = None if op.get("replace", True) else alpha
            m = iop.mask_from_color(rgb, float(op["x"]), float(op["y"]), float(op.get("tolerance", 18)),
                                    bool(op.get("invert")), base)
            note = f"魔棒选区占比约 {float((m > 0.5).mean()) * 100:.1f}%"
        else:                                            # mask_brush
            base = alpha if alpha is not None else np.ones((H, W), dtype=np.float32)
            m = iop.mask_draw_strokes(base, op["strokes"], W, H)
            note = f"已绘制 {len(op['strokes'])} 笔"

        rel, size = self._save_mask(layer["id"], m)
        rec = {"op": op["op"], "mask_file": rel, "mask_size": list(size)}
        for k in ("feather", "grow", "invert"):
            if k in op:
                rec[k] = op[k]
        layer["ops"].append(rec)
        return f"{note}；蒙版已存为 {rel}"

    def _save_mask(self, layer_id: str, mask: np.ndarray) -> tuple[str, tuple[int, int]]:
        from PIL import Image

        self.masks_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(self.masks_dir.glob(f"{layer_id}_*.png"))) + 1
        while (self.masks_dir / f"{layer_id}_{n}.png").exists():
            n += 1
        name = f"{layer_id}_{n}.png"
        u8 = (np.clip(mask, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        Image.fromarray(u8, mode="L").save(self.masks_dir / name)
        return f"masks/{name}", (int(mask.shape[1]), int(mask.shape[0]))

    # ---- 结构算子 ------------------------------------------------
    def _apply_structure(self, spec: ops.OpSpec, op: dict) -> str:
        name = op["op"]
        cw, ch = int(self.doc["canvas"]["w"]), int(self.doc["canvas"]["h"])

        if name == "add_layer":
            source = str(op["source"])
            x = float(op.get("x") if op.get("x") is not None else cw / 2.0)
            y = float(op.get("y") if op.get("y") is not None else ch / 2.0)
            scale = float(op.get("scale", 1.0))
            opacity = float(op.get("opacity", 1.0))
            blend = str(op.get("blend", "normal"))
            if source.startswith("asset:"):
                asset = self.asset(source.split(":", 1)[1])
                layer = self._new_layer(asset, x, y, op.get("name", ""), scale, opacity, blend)
            elif source.startswith("solid:"):
                color = source.split(":", 1)[1] or "#ffffff"
                asset = self._make_solid_asset(color, cw, ch)
                layer = self._new_layer(asset, x, y, op.get("name", "") or f"纯色{color}", scale, opacity, blend)
            elif source.startswith("cut:"):
                src = self.layer(source.split(":", 1)[1])
                new = copy.deepcopy(src)
                new["id"] = self._next_layer_id()
                new["name"] = (op.get("name") or f"{src['name']}·抠图")
                new["x"], new["y"] = x, y
                new["scale"], new["opacity"], new["blend"] = scale, opacity, blend
                self.doc["layers"].append(new)
                layer = new
            else:
                raise DocError("source 必须是 asset:<素材id> / solid:#rrggbb / cut:<图层id>")
            self.doc["active"] = layer["id"]
            return f"新增图层[{layer['id']}] {layer['name']}"

        if name == "duplicate_layer":
            src = self.layer(op["layer"])
            new = copy.deepcopy(src)
            new["id"] = self._next_layer_id()
            new["name"] = f"{src['name']}·副本"
            self.doc["layers"].append(new)
            self.doc["active"] = new["id"]
            return f"复制为[{new['id']}]"

        if name == "delete_layer":
            if len(self.doc["layers"]) <= 1:
                raise DocError("这是最后一个图层，删掉就没有内容了；如要清空请改用其他方式")
            idx = self.layer_index(op["layer"])
            gone = self.doc["layers"].pop(idx)
            if self.doc.get("active") == gone["id"]:
                self.doc["active"] = self.doc["layers"][max(0, idx - 1)]["id"]
            return f"已删除图层[{gone['id']}]"

        if name == "reorder_layer":
            idx = self.layer_index(op["layer"])
            layer = self.doc["layers"].pop(idx)
            to = op.get("to", "front")
            if to == "front":
                self.doc["layers"].append(layer)
            elif to == "back":
                self.doc["layers"].insert(0, layer)
            elif to == "up":
                self.doc["layers"].insert(min(len(self.doc["layers"]), idx + 1), layer)
            else:
                self.doc["layers"].insert(max(0, idx - 1), layer)
            return f"图层[{layer['id']}]移至{to}"

        if name == "set_layer":
            layer = self.layer(op["layer"])
            changed = []
            for k in ("opacity", "visible", "blend", "name"):
                if k in op and op[k] is not None:
                    layer[k] = op[k]
                    changed.append(k)
            return f"图层[{layer['id']}] 更新 {', '.join(changed) or '无'}"

        if name == "transform_layer":
            layer = self.layer(op["layer"])
            for k in ("x", "y", "scale", "rotation", "flip_h", "flip_v"):
                if k in op and op[k] is not None:
                    layer[k] = op[k]
            return f"图层[{layer['id']}] 变换已更新"

        if name == "align_layer":
            layer = self.layer(op["layer"])
            mode = op.get("mode", "center")
            ref = op.get("ref")
            lw, lh = self._layer_out_size(layer)
            if not ref or ref == "canvas":
                if mode == "center":
                    layer["x"], layer["y"] = cw / 2.0, ch / 2.0
                elif mode == "left":
                    layer["x"] = lw / 2.0
                elif mode == "right":
                    layer["x"] = cw - lw / 2.0
                elif mode == "top":
                    layer["y"] = lh / 2.0
                elif mode == "bottom":
                    layer["y"] = ch - lh / 2.0
            else:
                other = self.layer(ref)
                ox, oy = float(other.get("x", cw / 2)), float(other.get("y", ch / 2))
                ow, oh = self._layer_out_size(other)
                if mode == "center":
                    layer["x"], layer["y"] = ox, oy
                elif mode == "left":
                    layer["x"], layer["y"] = ox - ow / 2.0 - lw / 2.0, oy
                elif mode == "right":
                    layer["x"], layer["y"] = ox + ow / 2.0 + lw / 2.0, oy
                elif mode == "top":
                    layer["x"], layer["y"] = ox, oy - oh / 2.0 - lh / 2.0
                elif mode == "bottom":
                    layer["x"], layer["y"] = ox, oy + oh / 2.0 + lh / 2.0
            return f"图层[{layer['id']}] 对齐 {mode}（{round(layer['x'])},{round(layer['y'])}）"

        if name == "fit_layer":
            layer = self.layer(op["layer"])
            lw, lh = self._layer_out_size(layer, scale=1.0)
            mode = op.get("mode", "fit")
            if lw <= 0 or lh <= 0:
                raise DocError("图层尺寸异常")
            if mode == "fit":
                s = min(cw / lw, ch / lh)
            elif mode == "fill":
                s = max(cw / lw, ch / lh)
            else:
                s = 1.0
            layer["scale"] = float(s)
            layer["x"], layer["y"] = cw / 2.0, ch / 2.0
            return f"图层[{layer['id']}] {mode} 画幅，缩放={s:.3f}"

        if name == "remove_op":
            layer = self.layer(op["layer"])
            if not layer["ops"]:
                raise DocError(f"图层[{layer['id']}]没有任何已应用的操作")
            idx = int(op.get("index", -1))
            if idx < 0:
                idx = len(layer["ops"]) + idx
            if not (0 <= idx < len(layer["ops"])):
                raise DocError(f"index 越界：该图层有 {len(layer['ops'])} 步操作（从 0 计）")
            removed = layer["ops"].pop(idx)
            return f"已移除图层[{layer['id']}]第 {idx} 步 {removed.get('op')}"

        if name == "canvas":
            mode = op.get("mode", "resize")
            if op.get("bg") is not None:
                self.doc["canvas"]["bg"] = op["bg"] or None
            if mode == "fit" or (op.get("w") is None and op.get("h") is None):
                lw, lh = self._content_bounds()
                nw, nh = max(16, int(round(lw))), max(16, int(round(lh)))
            else:
                nw = int(op.get("w") or cw)
                nh = int(op.get("h") or ch)
            self._set_canvas(nw, nh, mode)
            # 用户一旦手动改过画布，就不在裁切后自动改它了（见 _maybe_fit_canvas_after_crop）
            self.doc["canvas"]["locked"] = True
            return f"画布 {nw}x{nh}（{mode}）"

        if name == "canvas_ratio":
            ratio = op.get("ratio", "1:1")
            if op.get("bg") is not None:
                self.doc["canvas"]["bg"] = op["bg"] or None
            a, b = (float(v) for v in ratio.split(":"))
            target = a / b
            cur = cw / ch
            if cur > target:
                nw, nh = cw, int(round(cw / target))
            else:
                nw, nh = int(round(ch * target)), ch
            self._set_canvas(nw, nh, "pad")
            self.doc["canvas"]["locked"] = True      # 同 canvas：用户手动定的画幅不再自动改
            return f"画布改为 {ratio} → {nw}x{nh}"

        if name == "apply_style":
            from . import styles as style_mod

            key = str(op.get("style") or "")
            # 注意：不能用 `op.get("strength", 100) or 100` —— 0 是 falsy，强度 0 会被改成 100
            raw_strength = op.get("strength")
            strength = 100.0 if raw_strength is None else float(raw_strength)
            try:
                style, expanded = style_mod.expand(key, strength)
            except KeyError:
                raise DocError(
                    f"没有风格 {key!r}；可用风格：{', '.join(style_mod.style_ids())}") from None
            layer = self.layer(op.get("layer"))
            for rec in expanded:
                layer["ops"].append({k: v for k, v in rec.items() if k != "layer"})
            self.doc["active"] = layer["id"]
            if not expanded:
                return f"风格「{style.name}」强度为 0，未做任何改动"
            return (f"已套用风格「{style.name}」（强度 {round(strength)}%）→ "
                    f"图层[{layer['id']}] 新增 {len(expanded)} 步，可逐步微调")

        if name == "export":
            path = self.export(op.get("format", "png"), int(op.get("quality", 95)),
                               int(op.get("max_side", 0) or 0), op.get("path"))
            self.last_export = str(path)
            return f"已导出：{path}"

        raise DocError(f"未实现的结构算子：{name}")

    # ---- 辅助 ----------------------------------------------------
    def _make_solid_asset(self, color: str, w: int, h: int) -> dict:
        from PIL import Image

        s = color.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            raise DocError(f"颜色格式应为 #rrggbb，收到 {color!r}")
        try:
            rgb = tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            raise DocError(f"颜色格式应为 #rrggbb，收到 {color!r}") from None
        n = len(self.doc["assets"]) + 1
        aid = f"A{n}"
        while (self.sources_dir / f"{aid}.png").exists():
            n += 1
            aid = f"A{n}"
        Image.new("RGB", (max(16, w), max(16, h)), rgb).save(self.sources_dir / f"{aid}.png")
        asset = {"id": aid, "name": f"纯色{color}", "file": f"sources/{aid}.png", "size": [int(w), int(h)]}
        self.doc["assets"].append(asset)
        return asset

    def _layer_out_size(self, layer: dict, scale: float | None = None) -> tuple[int, int]:
        w, h = iop.pipeline_size(tuple(layer.get("src_size") or (100, 100)), layer.get("ops") or [])
        s = float(layer.get("scale", 1.0) if scale is None else scale)
        if abs(float(layer.get("rotation", 0.0))) > 1e-4:
            w, h = iop.rotate_canvas_size(max(1, int(w * s)), max(1, int(h * s)), float(layer["rotation"]))
        else:
            w, h = max(1, int(round(w * s))), max(1, int(round(h * s)))
        return w, h

    def _content_bounds(self) -> tuple[float, float]:
        if not self.doc["layers"]:
            return float(self.doc["canvas"]["w"]), float(self.doc["canvas"]["h"])
        xs0, ys0, xs1, ys1 = [], [], [], []
        for l in self.doc["layers"]:
            if not l.get("visible", True):
                continue
            w, h = self._layer_out_size(l)
            xs0.append(float(l.get("x", 0)) - w / 2.0)
            ys0.append(float(l.get("y", 0)) - h / 2.0)
            xs1.append(float(l.get("x", 0)) + w / 2.0)
            ys1.append(float(l.get("y", 0)) + h / 2.0)
        if not xs0:
            return float(self.doc["canvas"]["w"]), float(self.doc["canvas"]["h"])
        return (max(xs1) - min(xs0), max(ys1) - min(ys0))

    def _set_canvas(self, nw: int, nh: int, mode: str) -> None:
        """resize=直接改尺寸；pad=改动尺寸但保持内容中心不动（内容坐标随之平移）。"""
        nw, nh = max(16, min(int(nw), 12000)), max(16, min(int(nh), 12000))
        old_w, old_h = int(self.doc["canvas"]["w"]), int(self.doc["canvas"]["h"])
        if mode == "fit" or mode == "pad":
            dx, dy = (nw - old_w) / 2.0, (nh - old_h) / 2.0
            for l in self.doc["layers"]:
                l["x"] = float(l.get("x", old_w / 2.0)) + dx
                l["y"] = float(l.get("y", old_h / 2.0)) + dy
        self.doc["canvas"]["w"], self.doc["canvas"]["h"] = nw, nh

    # ------------------------------------------------------------ 原图对比
    def render_original(self, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
        """渲染"原图基准"：**只保留几何算子**（裁切/旋转/校正/翻转/缩放），丢掉调色与蒙版。

        为什么不是直接拿磁盘上的源图：一旦做过裁切或校正，源图与成品的画幅不同，
        两者叠在一起"擦除对比"就对不齐了。丢掉调色与蒙版、保留几何，才能得到
        "同一取景下的未调色版本"，这才是能用来判断修图得失的对照。
        """
        doc = copy.deepcopy(self.doc)
        for l in doc.get("layers", []):
            l["ops"] = [o for o in (l.get("ops") or []) if o.get("op") in GEOMETRY_ONLY]
        return render.render_document(doc, self.dir, scale=scale)

    def edit_summary(self) -> list[dict]:
        """列出各图层真正改变了像素的步骤（供对比图与界面展示）。"""
        out = []
        for l in self.doc.get("layers", []):
            ops = [o for o in (l.get("ops") or []) if o.get("op") not in GEOMETRY_ONLY]
            if ops:
                out.append({"layer": l["id"], "name": l["name"],
                            "ops": [str(o.get("op")) for o in ops]})
        return out

    def is_edited(self) -> bool:
        return bool(self.edit_summary())

    def export_compare(self, max_side: int = 2400, fmt: str = "jpg", quality: int = 92,
                       gap: int = 18, path: str | None = None) -> Path:
        """导出「原图 | 成品」并列对比图，并附上所用步骤清单。"""
        from PIL import Image, ImageDraw, ImageFont

        cw, ch = int(self.doc["canvas"]["w"]), int(self.doc["canvas"]["h"])
        scale = 1.0
        if max_side and max(cw, ch) > max_side:
            scale = max_side / float(max(cw, ch))
        res_rgb, res_a = self.render_full(scale=scale)
        org_rgb, org_a = self.render_original(scale=scale)

        left = render.to_pil(render.flatten_on(org_rgb, org_a), None).convert("RGB")
        right = render.to_pil(render.flatten_on(res_rgb, res_a), None).convert("RGB")

        summary = self.edit_summary()
        ops_line = "；".join(f"{s['name']}: {', '.join(s['ops'])}" for s in summary) or "（未做调色类修改）"
        header = 46
        footer = 30
        W = left.width + right.width + gap * 3
        H = max(left.height, right.height) + header + footer + gap
        sheet = Image.new("RGB", (W, H), (18, 20, 26))
        sheet.paste(left, (gap, header))
        sheet.paste(right, (gap * 2 + left.width, header))

        font = None
        for cand in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhl.ttc",
                     r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf"):
            try:
                font = ImageFont.truetype(cand, 20)
                break
            except Exception:
                continue
        d = ImageDraw.Draw(sheet)
        zh = font is not None and "msyh" in str(getattr(font, "path", "") or "")
        d.text((gap, 12), "原图（仅几何校正）" if zh else "BEFORE", fill=(216, 161, 58), font=font)
        d.text((gap * 2 + left.width, 12), "成品" if zh else "AFTER", fill=(75, 179, 122), font=font)
        d.text((gap, header + left.height + 6), ("修改步骤：" if zh else "OPS: ") + ops_line,
               fill=(150, 158, 176), font=font)

        if path:
            out = Path(path)
            if not out.is_absolute():
                out = self.exports_dir / out
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = self.exports_dir / f"{stamp}_{safe_name(self.doc.get('name', 'compare'))}_对比.{fmt}"
        out.parent.mkdir(parents=True, exist_ok=True)
        kw: dict[str, Any] = {"quality": int(quality), "optimize": True, "subsampling": 0}
        sheet.save(out, **kw) if fmt == "jpg" else sheet.save(out)
        return out

    # ------------------------------------------------------------ 预览/导出
    def render_full(self, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
        """scale=1.0 为文档真实分辨率；预览用更小的 scale（见 preview_scale）。"""
        return render.render_document(self.doc, self.dir, scale=scale)

    def preview_scale(self) -> float:
        """预览渲染比例：把画布缩到预览最长边。24MP 上这让交互从 ~15s 降到 ~1s。"""
        max_side = int(self.settings.preview_max_side) if self.settings else 1400
        m = max(int(self.doc["canvas"]["w"]), int(self.doc["canvas"]["h"]))
        if m <= 0:
            return 1.0
        return min(1.0, max_side / float(m))

    def _push_preview(self) -> str:
        quality = int(self.settings.preview_quality) if self.settings else 88
        scale = self.preview_scale()
        rgb, alpha = self.render_full(scale=scale)      # 已是要显示的尺寸，无需二次缩放
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        out = self.preview_dir / "current.jpg"
        if float(alpha.min()) >= 0.999:
            render.to_pil(rgb).save(out, quality=quality, optimize=True)
        else:
            render.to_pil(render.flatten_on(rgb, alpha)).save(out, quality=quality, optimize=True)
        (self.preview_dir / "rev.txt").write_text(
            f"{self.doc.get('rev', 0)}|{scale:.4f}|{rgb.shape[1]}x{rgb.shape[0]}", encoding="utf-8")
        return "previews/current.jpg"

    def preview_path(self) -> Path:
        return self.preview_dir / "current.jpg"

    def export(self, fmt: str = "png", quality: int = 95, max_side: int = 0,
               path: str | None = None) -> Path:
        fmt = (fmt or "png").lower().lstrip(".")
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in ("png", "jpg", "webp"):
            raise DocError(f"导出格式只支持 png/jpg/webp，收到 {fmt!r}")

        cw, ch = int(self.doc["canvas"]["w"]), int(self.doc["canvas"]["h"])
        # 有尺寸上限时**直接按目标尺寸渲染**（与预览同一条已用等价性断言验证过的路径），
        # 而不是"全分辨率渲染 + 事后缩小" —— 后者白烧几倍时间
        scale = 1.0
        if max_side and max(cw, ch) > max_side:
            scale = max_side / float(max(cw, ch))
        rgb, alpha = self.render_full(scale=scale)

        transparent = float(alpha.min()) < 0.999
        if fmt == "png":
            pil = render.to_pil(rgb, alpha if transparent else None).convert("RGBA" if transparent else "RGB")
        else:
            # jpg/webp 无透明通道（webp 支持，但为通用性按白底压平）
            pil = render.to_pil(render.flatten_on(rgb, alpha)).convert("RGB")

        if path:
            out = Path(path)
            if not out.is_absolute():
                out = self.exports_dir / out
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = self.exports_dir / f"{stamp}_{safe_name(self.doc.get('name', 'export'))}.{fmt}"
        out.parent.mkdir(parents=True, exist_ok=True)

        kwargs: dict[str, Any] = {}
        if fmt == "jpg":
            kwargs = {"quality": int(min(100, max(1, quality))), "optimize": True, "subsampling": 0}
        elif fmt == "webp":
            kwargs = {"quality": int(min(100, max(1, quality))), "method": 6}
        pil.save(out, **kwargs)
        return out

    # ------------------------------------------------------------ 状态
    def state(self) -> dict:
        layers = []
        for l in self.doc["layers"]:
            w, h = self._layer_out_size(l)
            ops_list = []
            for i, o in enumerate(l.get("ops") or []):
                label = self._op_label(o)
                label["i"] = i
                ops_list.append(label)
            layers.append({
                "id": l["id"],
                "name": l["name"],
                "visible": bool(l.get("visible", True)),
                "opacity": round(float(l.get("opacity", 1.0)), 3),
                "blend": l.get("blend", "normal"),
                "x": round(float(l.get("x", 0)), 1),
                "y": round(float(l.get("y", 0)), 1),
                "scale": round(float(l.get("scale", 1.0)), 4),
                "rotation": round(float(l.get("rotation", 0.0)), 2),
                "flip_h": bool(l.get("flip_h")),
                "flip_v": bool(l.get("flip_v")),
                "src_size": list(l.get("src_size") or []),
                "out_size": [w, h],
                "has_mask": any(o.get("mask_file") for o in (l.get("ops") or [])),
                "ops": ops_list,
                "op_count": len(ops_list),
            })
        return {
            "name": self.doc.get("name", ""),
            "dir": str(self.dir),
            "canvas": dict(self.doc["canvas"]),
            "layers": layers,
            "active": self.doc.get("active", ""),
            "assets": [
                {"id": a["id"], "name": a["name"], "size": list(a["size"]),
                 "used": any(l.get("source") == a["file"] for l in self.doc["layers"]),
                 # RAW 溯源：让界面能标出来源与所用解码方式，不假装是 RAW 解码
                 **{k: a[k] for k in ("source_kind", "decoded", "stored", "note", "original")
                    if a.get(k)}}
                for a in self.doc["assets"]
            ],
            "rev": int(self.doc.get("rev", 0)),
            "undo_depth": len(self.undo),
            "redo_depth": len(self.redo),
            "preview_scale": round(self.preview_scale(), 4),
            "edited": self.is_edited(),
            "edit_summary": self.edit_summary(),
            "preview_url": f"/project/preview?v={self.doc.get('rev', 0)}&t={int(time.time())}",
            "original_url": f"/project/original?v={self.doc.get('rev', 0)}&t={int(time.time())}",
            "last_export": self.last_export,
            "chat": self.chat[-40:],
        }

    @staticmethod
    def _op_label(o: dict) -> dict:
        d = {k: v for k, v in o.items() if k not in ("mask_file", "mask_size")}
        if o.get("mask_file"):
            d["mask"] = "已生成"
        return d

    def llm_context(self, max_ops: int = 12) -> str:
        """把当前文档压缩成模型能读的文本（不含路径细节，含尺寸与已应用步骤）。"""
        c = self.doc["canvas"]
        out = [f"工程：{self.doc.get('name', '')}",
               f"画布：{c['w']}x{c['h']}，底色：{'透明' if not c.get('bg') else c['bg']}",
               f"图层（下层→上层，末尾=最上层）："]
        for l in self.doc["layers"]:
            w, h = self._layer_out_size(l)
            flags = []
            if not l.get("visible", True):
                flags.append("隐藏")
            if any(o.get("mask_file") for o in (l.get("ops") or [])):
                flags.append("有蒙版")
            line = (f"  [{l['id']}] {l['name']} 输出{w}x{h} 位置({round(float(l.get('x', 0)))},"
                    f"{round(float(l.get('y', 0)))}) 缩放{round(float(l.get('scale', 1.0)), 3)} "
                    f"旋转{round(float(l.get('rotation', 0.0)), 1)}° 不透明度{round(float(l.get('opacity', 1.0)), 2)} "
                    f"混合{l.get('blend', 'normal')}" + (f" ({'/'.join(flags)})" if flags else ""))
            out.append(line)
            ops_list = l.get("ops") or []
            if ops_list:
                shown = ops_list[-max_ops:]
                parts = []
                for i, o in enumerate(ops_list[-max_ops:], start=len(ops_list) - len(shown)):
                    parts.append(f"{i}.{o.get('op')}")
                out.append(f"      已应用步骤：{', '.join(parts)}")
        unused = [a for a in self.doc["assets"]
                  if not any(l.get("source") == a["file"] for l in self.doc["layers"])]
        if unused:
            out.append("可用素材（还没加入图层，可用 add_layer source=asset:<id> 使用）：" +
                       ", ".join(f"{a['id']}={a['name']}({a['size'][0]}x{a['size'][1]})" for a in unused))
        active = self.doc.get("active")
        if active:
            out.append(f"当前活动图层（不写 layer 时的默认目标）：{active}")
        return "\n".join(out)


def _flatten(rgb: np.ndarray, alpha: np.ndarray):
    return render.flatten_on(rgb, alpha), None
