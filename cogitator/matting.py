"""抠图：本机离线 BiRefNet（RMBG-2.0 权重）+ OpenCV GrabCut 兜底。

关键约束（实测）：
  * 本机 HuggingFace 不可达 → 必须完全离线加载；模型目录已存在则零下载。
  * 权重目录里的 birefnet.py 用的是相对导入 `from .BiRefNet_config import ...`，
    通用 AutoModelForImageSegmentation 会失败；这里在**内存中**改写这一行后 exec，
    绝不修改模型目录（既有 ComfyUI 节点会改写磁盘文件，我们不这么做）。
  * fp16 会因部分子模块未随之转换而报错（实测 RuntimeError），因此固定 fp32：
    1024 分辨率下单张约 1 秒，完全够用。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import numpy as np

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

_STATE_LOCK = threading.Lock()


class MattingUnavailable(RuntimeError):
    pass


def _pick_device(pref: str = "auto") -> str:
    if pref in ("cpu", "cuda"):
        return pref
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class MattingEngine:
    """懒加载的单例式引擎：首次调用才吃显存/内存。"""

    def __init__(self, model_dir: str = "", device: str = "auto", resolution: int = 1024) -> None:
        self.model_dir = model_dir
        self.device_pref = device
        self.resolution = int(resolution)
        self._model = None
        self._device = ""
        self._load_error = ""
        self._load_seconds = 0.0
        self._last_engine = ""

    # -- 状态 ------------------------------------------------------
    def status(self) -> dict[str, Any]:
        d = self.model_dir
        ready = bool(d) and Path(d, "birefnet.py").is_file() and Path(d, "model.safetensors").is_file()
        return {
            "model_dir": d,
            "weights_found": ready,
            "loaded": self._model is not None,
            "device": self._device or self.device_pref,
            "resolution": self.resolution,
            "load_seconds": round(self._load_seconds, 2),
            "last_engine": self._last_engine,
            "error": self._load_error,
            "grabcut_available": _has_cv2(),
        }

    def release(self) -> None:
        with _STATE_LOCK:
            self._model = None
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    # -- BiRefNet --------------------------------------------------
    def _load(self):
        if self._model is not None:
            return self._model
        with _STATE_LOCK:
            if self._model is not None:
                return self._model
            d = Path(self.model_dir or "")
            cfg_path = d / "BiRefNet_config.py"
            net_path = d / "birefnet.py"
            w_path = d / "model.safetensors"
            if not (cfg_path.is_file() and net_path.is_file() and w_path.is_file()):
                raise MattingUnavailable(
                    f"未找到 BiRefNet 权重目录（需要 birefnet.py / BiRefNet_config.py / model.safetensors）：{d}"
                )
            t0 = time.time()
            import torch
            from safetensors.torch import load_file

            # 1) 先按文件路径装载配置类
            spec = importlib.util.spec_from_file_location("BiRefNet_config", str(cfg_path))
            cfg_mod = importlib.util.module_from_spec(spec)
            sys.modules["BiRefNet_config"] = cfg_mod
            spec.loader.exec_module(cfg_mod)

            # 2) 在内存里把相对导入改成绝对导入，再 exec（不落盘、不改模型目录）
            src = net_path.read_text(encoding="utf-8")
            src = src.replace("from .BiRefNet_config import", "from BiRefNet_config import")
            net_mod = types.ModuleType("cogitator_birefnet")
            net_mod.__file__ = str(net_path)
            sys.modules["cogitator_birefnet"] = net_mod
            exec(compile(src, str(net_path), "exec"), net_mod.__dict__)

            # 3) 实例化并灌权重（bb_pretrained=False 才不会去联网下载骨干）
            config = cfg_mod.BiRefNetConfig()
            model = net_mod.BiRefNet(bb_pretrained=False, config=config)
            state = load_file(str(w_path))
            missing, unexpected = model.load_state_dict(state, strict=False)
            if missing:
                raise MattingUnavailable(f"权重不匹配：缺 {len(missing)} 项，例如 {missing[:3]}")
            model.eval()
            self._device = _pick_device(self.device_pref)
            model.to(self._device)
            try:
                torch.set_float32_matmul_precision("high")
            except Exception:
                pass
            self._model = model
            self._load_seconds = time.time() - t0
            self._load_error = ""
            self._last_engine = "birefnet"
            return model

    def _birefnet_alpha(self, img: Any) -> np.ndarray:
        import torch
        from torchvision import transforms

        model = self._load()
        res = self.resolution
        tf = transforms.Compose([
            transforms.Resize((res, res), interpolation=transforms.InterpolationMode.BICUBIC),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
        w, h = img.size
        x = tf(img).unsqueeze(0).to(self._device, dtype=torch.float32)
        with torch.no_grad():
            preds = model(x)
        pred = preds[-1].sigmoid().float().cpu()[0].squeeze()
        mask = transforms.ToPILImage()(pred).resize((w, h), 1)   # 1 = BILINEAR
        return np.asarray(mask, dtype=np.float32) / 255.0

    # -- GrabCut 兜底 ----------------------------------------------
    def _grabcut_alpha(self, img: Any) -> np.ndarray:
        if not _has_cv2():
            raise MattingUnavailable("GrabCut 需要 opencv-python，未安装")
        import cv2

        arr = np.asarray(img.convert("RGB"))
        h, w = arr.shape[:2]
        scale = 1.0
        if max(h, w) > 1600:                     # GrabCut 很吃时间，先降采样
            scale = 1600.0 / max(h, w)
            arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        hh, ww = arr.shape[:2]
        mask = np.zeros((hh, ww), np.uint8)
        rect = (int(ww * 0.06), int(hh * 0.06), int(ww * 0.88), int(hh * 0.88))
        bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.grabCut(arr, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
        alpha = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 1.0, 0.0).astype(np.float32)
        # 只保留最大连通域，去掉零碎误检
        try:
            from scipy import ndimage  # type: ignore

            lab, n = ndimage.label(alpha > 0.5)
            if n > 1:
                sizes = ndimage.sum(alpha > 0.5, lab, range(1, n + 1))
                keep = int(np.argmax(sizes)) + 1
                alpha = np.where(lab == keep, 1.0, 0.0).astype(np.float32)
        except Exception:
            pass
        if scale != 1.0:
            alpha = cv2.resize(alpha, (w, h), interpolation=cv2.INTER_LINEAR)
        # 轻微羽化，避免锯齿
        try:
            alpha = cv2.GaussianBlur(alpha, (0, 0), 1.2)
        except Exception:
            pass
        return alpha

    # -- 对外 ------------------------------------------------------
    def alpha(self, img: Any, engine: str = "auto") -> tuple[np.ndarray, str]:
        """返回 (alpha HxW float32 0~1, 实际使用的引擎名)。"""
        want = (engine or "auto").lower()
        if want in ("birefnet", "auto"):
            try:
                return self._birefnet_alpha(img), "birefnet"
            except Exception as e:
                self._load_error = f"{type(e).__name__}: {e}"
                if want == "birefnet":
                    raise
                # auto 模式下降级
                self._last_engine = "grabcut(fallback)"
        a = self._grabcut_alpha(img)
        self._last_engine = "grabcut" if want != "auto" else "grabcut(降级)"
        return a, self._last_engine


def _has_cv2() -> bool:
    try:
        import cv2  # noqa: F401

        return True
    except Exception:
        return False


_ENGINES: dict[tuple[str, str, int], MattingEngine] = {}


def get_engine(model_dir: str, device: str = "auto", resolution: int = 1024) -> MattingEngine:
    key = (str(model_dir), str(device), int(resolution))
    if key not in _ENGINES:
        _ENGINES[key] = MattingEngine(model_dir, device, resolution)
    return _ENGINES[key]


def remove_background(img: Any, model_dir: str = "", engine: str = "auto",
                      device: str = "auto", resolution: int = 1024) -> tuple[np.ndarray, str]:
    eng = get_engine(model_dir, device, resolution)
    return eng.alpha(img, engine)
