"""算子注册表 —— 本项目的唯一契约来源。

同一个 SPECS 同时负责三件事：
  1. 校验 AI 产出的编辑指令（类型、范围、枚举、必填）；
  2. 生成给模型看的《算子手册》（模型看到的可用动作永远等于本机真正会执行的动作）；
  3. 索引实现函数（管线算子 → image_ops，结构算子 → document）。

因此不存在"模型编出一个本机没有的操作"这类漂移：未知算子直接判非法并回灌错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------- 参数与规格


@dataclass
class Param:
    name: str
    kind: str                       # number | int | bool | str | points | strokes | ref
    desc: str
    default: Any = None
    lo: float | None = None
    hi: float | None = None
    enum: list[str] | None = None
    required: bool = False
    optional: bool = False          # 可选：不传就完全不动这个属性

    def describe(self) -> str:
        bits: list[str] = []
        if self.kind == "number":
            rng = []
            if self.lo is not None:
                rng.append(f"{_fmt(self.lo)}")
            if self.hi is not None:
                rng.append(f"{_fmt(self.hi)}")
            if rng:
                bits.append("~".join(rng))
        elif self.kind == "int":
            rng = []
            if self.lo is not None:
                rng.append(str(int(self.lo)))
            if self.hi is not None:
                rng.append(str(int(self.hi)))
            if rng:
                bits.append("~".join(rng))
        elif self.kind == "bool":
            bits.append("true/false")
        elif self.kind == "str" and self.enum:
            bits.append("|".join(self.enum))
        elif self.kind == "points":
            bits.append("[[x,y],...] 0~255")
        elif self.kind == "strokes":
            bits.append("[{mode,radius,points}]")
        elif self.kind == "ref":
            bits.append("图层id 或 canvas")
        if self.required:
            bits.append("必填")
        elif self.optional:
            bits.append("可选")
        if self.default is not None and self.kind not in ("strokes", "points"):
            bits.append(f"默认{_fmt(self.default)}")
        suffix = f"({', '.join(bits)})" if bits else ""
        return f"{self.name}{suffix} {self.desc}"


def _fmt(v: Any) -> str:
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


@dataclass
class OpSpec:
    name: str
    category: str                   # 调色 | 几何 | 抠图 | 图层 | 画布 | 输出
    desc: str
    params: list[Param] = field(default_factory=list)
    kind: str = "pipeline"          # pipeline = 追加到图层管线；structure = 立即改动结构
    layer: bool = True              # 是否用 layer 字段指定目标图层
    example: str = ""
    caution: str = ""

    def signature(self) -> str:
        return self.name + "".join(
            f" {p.name}" for p in self.params
        )

    def param_map(self) -> dict[str, Param]:
        return {p.name: p for p in self.params}


def num(name: str, desc: str, lo: float, hi: float, default: float | None = None,
        required: bool = False) -> Param:
    return Param(name, "number", desc, default, lo, hi, required=required)


def integer(name: str, desc: str, lo: int, hi: int, default: int | None = None,
            required: bool = False) -> Param:
    return Param(name, "int", desc, default, lo, hi, required=required)


def flag(name: str, desc: str, default: bool = False) -> Param:
    return Param(name, "bool", desc, default)


def opt_flag(name: str, desc: str) -> Param:
    """可选布尔：不给就完全不动这个属性（避免"移动图层顺手把翻转重置了"）。"""
    return Param(name, "bool", desc, None)


def opt_num(name: str, desc: str, lo: float, hi: float) -> Param:
    """可选数值：不给就保持不变（或由实现取合理缺省）。"""
    return Param(name, "number", desc, None, lo, hi, optional=True)


def opt_int(name: str, desc: str, lo: int, hi: int) -> Param:
    return Param(name, "int", desc, None, lo, hi, optional=True)


def opt_ref(name: str, desc: str) -> Param:
    return Param(name, "ref", desc, None, optional=True)


def text(name: str, desc: str, enum: list[str] | None = None, default: str | None = None,
         required: bool = False) -> Param:
    return Param(name, "str", desc, default, enum=enum, required=required)


def ref(name: str, desc: str, required: bool = False) -> Param:
    return Param(name, "ref", desc, required=required)


# ---------------------------------------------------------------- 强度约定
# 统一刻度，避免模型乱猜数值：
#   偏色/明暗类 -100~100：±10 轻微、±30 明显、±60 强烈、±100 极限
#   曝光 EV -3~3：0.1 轻微、0.3 明显、1.0 大约一档
#   滤镜强度 0~100

COLOR_RANGE = "-100~100（±10轻微 ±30明显 ±60强烈）"


SPECS: list[OpSpec] = []


def _spec(*args, **kwargs) -> None:
    SPECS.append(OpSpec(*args, **kwargs))


# ---- 调色 ---------------------------------------------------------------
_spec("exposure", "调色", "整体曝光，单位 EV（+ 变亮）。0.1 轻微、0.3 明显、1.0 约一档",
      [num("value", "EV", -3, 3, required=True)],
      example='{"op":"exposure","value":0.3}')
_spec("brightness", "调色", "亮度（比曝光更粗暴，会压到底/顶到白）",
      [num("value", "强度", -100, 100, required=True)])
_spec("contrast", "调色", "对比度，以中灰 0.5 为支点",
      [num("value", "强度", -100, 100, required=True)])
_spec("saturation", "调色", "饱和度，-100 = 完全去色",
      [num("value", "强度", -100, 100, required=True)])
_spec("vibrance", "调色", "自然饱和度：只提升欠饱和区域，不易过曝肤色",
      [num("value", "强度", -100, 100, required=True)])
_spec("temperature", "调色", "色温：正=暖（加黄减蓝），负=冷",
      [num("value", "强度", -100, 100, required=True)])
_spec("tint", "调色", "色调/品红-绿：正=偏品红，负=偏绿",
      [num("value", "强度", -100, 100, required=True)])
_spec("highlights", "调色", "高光恢复：负值压暗高光找回过曝细节",
      [num("value", "强度", -100, 100, required=True)])
_spec("shadows", "调色", "暗部提亮：正值提亮阴影找回暗部细节",
      [num("value", "强度", -100, 100, required=True)])
_spec("whites", "调色", "白场：调最亮点（与 levels.white 类似但更柔和）",
      [num("value", "强度", -100, 100, required=True)])
_spec("blacks", "调色", "黑场：调最暗点，负值加深黑位",
      [num("value", "强度", -100, 100, required=True)])
_spec("levels", "调色", "色阶：黑场/白场/伽马。black 抬到 20 会把暗部切掉",
      [num("black", "黑场 0~254", 0, 254, 0), num("white", "白场 1~255", 1, 255, 255),
       num("gamma", "伽马", 0.1, 5.0, 1.0)])
_spec("curve", "调色", "曲线：控制点 [[输入,输出],...]，0~255，至少 2 点，按输入排序",
      [text("channel", "通道", ["rgb", "r", "g", "b"], "rgb"),
       Param("points", "points", "控制点列表", required=True)])
_spec("hue", "调色", "色相旋转（度）",
      [num("value", "角度", -180, 180, required=True)])
_spec("grayscale", "调色", "转黑白（保持亮度权重）", [])
_spec("sepia", "调色", "棕褐色调，强度 0~100",
      [num("value", "强度", 0, 100, 60)])
_spec("clarity", "调色", "清晰度/局部对比：增强中间调层次（人像慎用 >40）",
      [num("value", "强度", -100, 100, required=True)])
_spec("sharpen", "调色", "锐化（USM）",
      [num("value", "强度", 0, 100, 50), num("radius", "半径像素", 0.5, 5, 1.2)])
_spec("blur", "调色", "高斯模糊，常用作背景虚化",
      [num("value", "强度", 0, 100, required=True)])
_spec("denoise", "调色", "降噪（双边滤波，保边去噪），适合高感光噪点",
      [num("value", "强度", 0, 100, 50)])
_spec("auto_enhance", "调色", "一键自动增强：自动白平衡 + 自动色阶 + 轻微对比与锐化",
      [num("strength", "强度", 0, 100, 70)])
_spec("hsl", "调色", "分区 HSL：只调整某个色相带（比无脑拉全局饱和度专业得多）",
      [text("color", "色相带", ["red", "orange", "yellow", "green", "aqua", "blue",
                                "purple", "magenta"], "red"),
       num("hue", "色相偏移角度（如绿→黄 = 绿带 -15）", -60, 60, 0),
       num("saturation", "该色带饱和度", -100, 100, 0),
       num("luminance", "该色带明度（-100 压到近黑）", -100, 100, 0),
       num("width", "影响范围（度，越大牵连越多）", 10, 80, 45)],
      example='{"op":"hsl","color":"green","hue":-12,"saturation":-15,"luminance":-6}')
_spec("split_tone", "调色", "分离色调：高光加一个色、阴影加另一个色（电影感/胶片感的核心手法）",
      [num("highlight_hue", "高光色相 0-360（45=暖黄，200=冷青）", 0, 360, 45),
       num("highlight_sat", "高光着色强度", 0, 100, 12),
       num("shadow_hue", "阴影色相 0-360（210=青蓝，280=紫）", 0, 360, 210),
       num("shadow_sat", "阴影着色强度", 0, 100, 12),
       num("balance", "平衡：正=偏高高光，负=偏低阴影", -100, 100, 0)],
      example='{"op":"split_tone","highlight_hue":42,"highlight_sat":16,"shadow_hue":205,"shadow_sat":20}')
_spec("vignette", "调色", "暗角：正值压暗四角收拢视线，负值提亮四角（白色晕影通常显得廉价，慎用）",
      [num("amount", "强度（正=压暗）", -60, 80, 18),
       num("feather", "过渡柔和度", 5, 95, 55)])
_spec("grain", "调色", "胶片颗粒：中间调最明显，高光暗部收敛（固定种子，同一文档渲染结果稳定）",
      [num("amount", "颗粒量", 0, 80, 12), num("size", "颗粒尺寸", 0.4, 5, 1.2)])
_spec("apply_style", "调色", "套用风格配方：把一整套算子按顺序追加到图层（推荐先套风格再微调）",
      [text("style", "风格 id（见风格清单，如 jp-airy / film-warm / cinematic / bw-hard）",
            None, required=True),
       num("strength", "强度 0~150（100=原配方，50=半量）", 0, 150, 100)],
      kind="structure",
      example='{"op":"apply_style","style":"jp-airy","strength":80}',
      caution="套用后每个算子都会出现在图层管线里，可单独微调；不要连续套两个风格。")

# ---- 几何（作用于图层栅格，与蒙版同步变换）-------------------------------
_spec("crop", "几何", "裁切。x,y 为左上角，w,h 为宽高，单位=该图层当前像素",
      [integer("x", "左", 0, 100000, 0), integer("y", "上", 0, 100000, 0),
       integer("w", "宽", 1, 100000, required=True), integer("h", "高", 1, 100000, required=True)])
_spec("crop_ratio", "几何", "按比例裁切（居中或贴边）",
      [text("ratio", "比例", ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16", "2:1", "1:2"], "1:1"),
       text("anchor", "锚点", ["center", "top", "bottom", "left", "right"], "center")])
_spec("rotate", "几何", "旋转（度，正=顺时针）。expand=true 时扩大画幅以免切角",
      [num("angle", "角度", -180, 180, required=True), flag("expand", "扩大画幅", True)])
_spec("straighten", "几何", "校正倾斜：小幅旋转并自动内切裁掉黑边",
      [num("angle", "角度", -15, 15, required=True)])
_spec("flip", "几何", "镜像翻转",
      [text("axis", "轴", ["h", "v"], "h")])
_spec("resize", "几何", "缩放栅格。只给 w 或 h 时按比例；也可给 scale",
      [opt_int("w", "宽", 1, 20000), opt_int("h", "高", 1, 20000),
       opt_num("scale", "倍率", 0.05, 8)])

# ---- 抠图 ---------------------------------------------------------------
_spec("remove_bg", "抠图", "一键抠图/去背景：本机 BiRefNet（RMBG-2.0）出 alpha，失败自动退 GrabCut",
      [text("engine", "引擎", ["auto", "birefnet", "grabcut"], "auto"),
       num("feather", "边缘羽化 0~20", 0, 20, 1.0),
       num("grow", "蒙版收缩(-)/扩张(+) 像素", -20, 20, 0),
       flag("invert", "反选", False)],
      kind="mask",
      caution="抠图会耗时 1~3 秒，一次调用就够，不要重复调用。")
_spec("clear_mask", "抠图", "清空该图层的蒙版（恢复全不透明）", [], kind="mask")
_spec("mask_brush", "抠图", "手绘蒙版：points 为归一化坐标 0~1，可多次调用补细节",
      [Param("strokes", "strokes", "笔画列表", required=True),
       num("feather", "羽化", 0, 20, 1.0)],
      kind="mask",
      example='{"op":"mask_brush","strokes":[{"mode":"erase","radius":0.06,"points":[[0.5,0.4]]}]}',
      caution="radius 为归一化半径（画幅短边的比例），普通修补 0.02~0.08。")
_spec("mask_from_color", "抠图", "魔棒：以某点颜色为准生成/追加蒙版（适合纯色背景）",
      [num("x", "点x 归一化", 0, 1, required=True), num("y", "点y 归一化", 0, 1, required=True),
       num("tolerance", "容差 0~100", 0, 100, 18),
       flag("invert", "反选", False), flag("replace", "替换现有蒙版", True)],
      kind="mask")

# ---- 图层 ---------------------------------------------------------------
_spec("add_layer", "图层", "新增图层：可加图片、纯色、或把某图层抠出的主体作为新图层",
      [text("source", "来源：asset:<资产id> 或 solid:#rrggbb 或 cut:<图层id>（cut=复制该图层当前处理结果）",
            None, required=True),
       text("name", "图层名", None, None),
       opt_num("x", "画布位置x（省略=画布中心）", -20000, 20000),
       opt_num("y", "画布位置y（省略=画布中心）", -20000, 20000),
       num("scale", "缩放倍率", 0.02, 10, 1.0),
       num("opacity", "不透明度 0~1", 0, 1, 1.0),
       text("blend", "混合模式", ["normal", "multiply", "screen", "overlay", "darken",
                                 "lighten", "add", "soft_light", "difference"], "normal")],
      kind="structure", layer=False)
_spec("duplicate_layer", "图层", "复制图层",
      [opt_ref("layer", "源图层（省略=当前活动图层）")], kind="structure")
_spec("delete_layer", "图层", "删除图层（破坏性操作，必须写明要删哪个图层，不允许省略）",
      [ref("layer", "目标图层（必填）", required=True)], kind="structure")
_spec("reorder_layer", "图层", "调整图层前后顺序（数组末尾=最上层）",
      [opt_ref("layer", "目标图层（省略=当前活动图层）"),
       text("to", "位置", ["front", "back", "up", "down"], "front")], kind="structure")
_spec("set_layer", "图层", "改图层属性：名称/不透明度/显隐/混合模式（只传要改的字段）",
      [opt_ref("layer", "目标图层（省略=当前活动图层）"),
       opt_num("opacity", "不透明度 0~1", 0, 1),
       opt_flag("visible", "可见"),
       text("blend", "混合模式", ["normal", "multiply", "screen", "overlay", "darken",
                                 "lighten", "add", "soft_light", "difference"], None),
       text("name", "名称", None, None)], kind="structure")
_spec("transform_layer", "图层", "移动/缩放/旋转图层（画布坐标系，位置=图层中心；只传要改的字段）",
      [opt_ref("layer", "目标图层（省略=当前活动图层）"),
       opt_num("x", "中心x", -20000, 20000), opt_num("y", "中心y", -20000, 20000),
       opt_num("scale", "倍率", 0.02, 10), opt_num("rotation", "旋转角度", -180, 180),
       opt_flag("flip_h", "水平翻转"), opt_flag("flip_v", "垂直翻转")], kind="structure")
_spec("align_layer", "图层", "对齐：把图层对齐到画布或另一图层",
      [opt_ref("layer", "目标图层（省略=当前活动图层）"),
       text("mode", "方式", ["center", "left", "right", "top", "bottom"], "center"),
       opt_ref("ref", "参照物：canvas（默认）或图层id")], kind="structure")
_spec("fit_layer", "图层", "适配画幅：fit=完整放入留边，fill=铺满裁切，original=原始尺寸居中",
      [opt_ref("layer", "目标图层（省略=当前活动图层）"),
       text("mode", "方式", ["fit", "fill", "original"], "fit")], kind="structure")
_spec("remove_op", "图层", "从图层管线上删掉某一步（index 用 -1 表示最后一步），用于「刚才那步不要」",
      [opt_ref("layer", "目标图层（省略=当前活动图层）"),
       integer("index", "序号，-1=最后", -50, 200, -1)], kind="structure")

# ---- 画布 ---------------------------------------------------------------
_spec("canvas", "画布", "设置画布尺寸与底色。mode: resize=直接改尺寸；fit=按当前内容适配；pad=扩边并保持内容居中",
      [opt_int("w", "宽", 16, 12000), opt_int("h", "高", 16, 12000),
       text("mode", "方式", ["resize", "fit", "pad"], "resize"),
       text("bg", "底色（#rrggbb；空字符串=透明，不传=不变）", None, None)], kind="structure", layer=False)
_spec("canvas_ratio", "画布", "把画布改成指定比例（内容不动，仅扩/裁画幅）",
      [text("ratio", "比例", ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"], "1:1"),
       text("bg", "底色", None, None)], kind="structure", layer=False)

# ---- 输出 ---------------------------------------------------------------
_spec("export", "输出", "导出成品图到工程 exports 目录",
      [text("format", "格式", ["png", "jpg", "webp"], "png"),
       integer("quality", "质量 1~100", 1, 100, 95),
       integer("max_side", "最长边像素上限（0=原尺寸）", 0, 12000, 0),
       text("path", "另存到指定路径（可选，相对路径放到 exports/）", None, None)],
      kind="structure", layer=False)


SPEC_BY_NAME: dict[str, OpSpec] = {s.name: s for s in SPECS}

BLEND_MODES = ["normal", "multiply", "screen", "overlay", "darken", "lighten",
               "add", "soft_light", "difference"]


# ---------------------------------------------------------------- 风格配方

# 套用风格时按 strength 缩放哪些参数：只缩放"强度类"，不动形状类（feather/size/radius/
# width）与绝对点（levels 的黑白场、gamma 的 1.0 中性值），否则强度一改就把风格的方向
# 甚至合法性也改掉了。gamma 由 styles.NEUTRAL 单独按 1.0 做中性插值。
STYLE_SCALE_KEYS = {
    "value", "amount", "strength", "saturation", "luminance", "contrast",
    "shadows", "highlights", "whites", "blacks", "clarity", "vibrance",
    "temperature", "tint", "hue", "denoise", "blur", "sepia", "grain",
}


def style_scalable(param_name: str) -> bool:
    if param_name.endswith("_hue"):
        return False
    if param_name.endswith("_sat"):
        return True
    return param_name in STYLE_SCALE_KEYS


# ---------------------------------------------------------------- 校验

class OpError(ValueError):
    """算子非法：附带可回灌给模型的说明。"""


def _coerce(p: Param, raw: Any, op_name: str) -> Any:
    if p.kind in ("number", "int"):
        if isinstance(raw, bool) or raw is None:
            raise OpError(f"{op_name}.{p.name} 需要数字，收到 {raw!r}")
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise OpError(f"{op_name}.{p.name} 需要数字，收到 {raw!r}") from None
        if p.kind == "int":
            v = int(round(v))
        if p.lo is not None and v < p.lo:
            raise OpError(f"{op_name}.{p.name}={v} 超出下限 {_fmt(p.lo)}")
        if p.hi is not None and v > p.hi:
            raise OpError(f"{op_name}.{p.name}={v} 超出上限 {_fmt(p.hi)}")
        return v
    if p.kind == "bool":
        return bool(raw)
    if p.kind == "str":
        s = raw if isinstance(raw, str) else str(raw)
        if p.enum and s not in p.enum:
            raise OpError(f"{op_name}.{p.name}={s!r} 不在允许值 {p.enum}")
        return s
    if p.kind == "points":
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise OpError(f"{op_name}.{p.name} 需要至少 2 个 [x,y] 控制点")
        out = []
        for pt in raw:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise OpError(f"{op_name}.{p.name} 每个点必须是 [x,y]")
            x = min(255.0, max(0.0, float(pt[0])))
            y = min(255.0, max(0.0, float(pt[1])))
            out.append([x, y])
        out.sort(key=lambda t: t[0])
        return out
    if p.kind == "strokes":
        if not isinstance(raw, (list, tuple)) or not raw:
            raise OpError(f"{op_name}.{p.name} 需要非空笔画列表")
        out = []
        for st in raw:
            if not isinstance(st, dict):
                raise OpError(f"{op_name}.{p.name} 每笔必须是对象")
            mode = str(st.get("mode", "erase"))
            if mode not in ("add", "erase"):
                raise OpError(f"{op_name}.strokes.mode 必须是 add 或 erase，收到 {mode!r}")
            pts = st.get("points") or []
            if not pts:
                raise OpError(f"{op_name}.strokes.points 不能为空")
            norm = []
            for pt in pts:
                if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                    raise OpError(f"{op_name}.strokes.points 每点必须是 [x,y] 归一化坐标")
                norm.append([min(1.0, max(0.0, float(pt[0]))), min(1.0, max(0.0, float(pt[1])))])
            radius = float(st.get("radius", 0.04))
            radius = min(0.5, max(0.001, radius))
            out.append({"mode": mode, "radius": radius, "points": norm})
        return out
    if p.kind == "ref":
        if raw is None:
            raise OpError(f"{op_name}.{p.name} 不能为空")
        return str(raw)
    return raw


def validate_op(raw: Any, index: int = 0) -> dict[str, Any]:
    """把一个原始 op 归一化成可执行指令；非法则抛 OpError（信息可直接给模型看）。"""
    if not isinstance(raw, dict):
        raise OpError(f"第{index + 1}条指令不是对象：{raw!r}")
    name = raw.get("op") or raw.get("name")
    if not isinstance(name, str) or name not in SPEC_BY_NAME:
        near = ", ".join(s.name for s in SPECS)
        raise OpError(f"未知操作 {name!r}。可用操作只有：{near}")
    spec = SPEC_BY_NAME[name]
    out: dict[str, Any] = {"op": name}
    pmap = spec.param_map()
    for key in raw:
        if key in ("op", "name", "layer", "note"):
            continue
        if key not in pmap:
            allowed = ", ".join(pmap) or "（无参数）"
            raise OpError(f"{name} 不认识参数 {key!r}，可用参数：{allowed}")
    for p in spec.params:
        present = p.name in raw and raw[p.name] is not None
        if not present:
            if p.required or (p.default is None and not p.optional
                              and p.kind in ("number", "int", "ref", "points", "strokes")):
                raise OpError(f"{name} 缺少必填参数 {p.name}（{p.desc}）")
            if p.default is not None:
                out[p.name] = p.default
            continue
        out[p.name] = _coerce(p, raw[p.name], name)
    if spec.layer:
        layer = raw.get("layer")
        if layer is None and spec.kind in ("pipeline", "mask"):
            out["layer"] = None          # 渲染时落到当前活动图层
        elif layer is not None:
            out["layer"] = str(layer)
    if spec.kind == "structure" and spec.name in ("canvas", "canvas_ratio", "export", "add_layer"):
        out.pop("layer", None)
    if raw.get("note"):
        out["note"] = str(raw["note"])[:200]
    return out


def validate_ops(raw_list: Any, max_ops: int = 40) -> list[dict[str, Any]]:
    if raw_list is None:
        return []
    if isinstance(raw_list, dict):
        raw_list = [raw_list]
    if not isinstance(raw_list, list):
        raise OpError("ops 必须是数组")
    if len(raw_list) > max_ops:
        raise OpError(f"一次最多 {max_ops} 条指令，收到 {len(raw_list)} 条，请拆成多轮")
    return [validate_op(o, i) for i, o in enumerate(raw_list)]


# ---------------------------------------------------------------- 手册生成

_RANGE_NOTE: dict[str, str] = {
    "调色": f"强度刻度统一为 {COLOR_RANGE}；曝光用 EV。允许小数（如 0.25）。",
    "几何": "坐标单位是该图层自身的像素；不确定尺寸时优先 crop_ratio / straighten / flip。",
    "抠图": "抠图较慢（1~3 秒），确认需要时才调用；mask_brush 可反复调用补细节。",
    "图层": "图层数组末尾是最上层；layer 字段填图层 id。",
    "画布": "改动画布会影响所有图层的相对位置。",
    "输出": "只有用户明确说要导出/保存时才调用 export。",
}


def manual(categories: list[str] | None = None, include_examples: bool = True) -> str:
    """生成给模型看的算子手册。"""
    cats = categories or ["调色", "几何", "抠图", "图层", "画布", "输出"]
    lines: list[str] = []
    for cat in cats:
        specs = [s for s in SPECS if s.category == cat]
        if not specs:
            continue
        lines.append(f"【{cat}】{_RANGE_NOTE.get(cat, '')}")
        for s in specs:
            params = "，".join(p.describe() for p in s.params) or "无参数"
            extra = f" ⚠{s.caution}" if s.caution else ""
            lines.append(f"  - {s.name}: {s.desc} | {params}{extra}")
            if include_examples and s.example:
                lines.append(f"      例：{s.example}")
        lines.append("")
    return "\n".join(lines).strip()


def json_example() -> str:
    return (
        "[\n"
        '  {"op":"auto_enhance","strength":60},\n'
        '  {"op":"temperature","value":-12},\n'
        '  {"op":"shadows","value":25},\n'
        '  {"op":"crop_ratio","ratio":"4:3","anchor":"center"}\n'
        "]"
    )


def json_schema_hint() -> str:
    return (
        '{"diagnosis": "看图诊断：题材、主体、具体问题与位置", '
        '"intent": "本轮要达成的审美意图（一句话）", '
        '"reply": "给用户看的说明：做了什么、幅度多少、为什么", '
        '"ops": [ ...编辑指令数组... ], '
        '"load": ["需要加载的参考id，最多2个"], '
        '"question": "需要用户确认时的问题，没有就留空"}'
    )


def json_schema_hint_plus() -> str:
    """与 json_schema_hint 同一份契约（保留别名给提示词用）。"""
    return json_schema_hint()
