"""配置：模型档案（含默认 DeepSeek）、抠图与预览参数。

settings.json 是唯一的人可编辑配置入口，模型密钥只存在本机这个文件里
（或经 api_key_env 指向的环境变量），不会写进项目文档、日志或对话记录。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import SETTINGS_FILE, ensure_dirs

# ---------------------------------------------------------------- 模型档案

@dataclass
class ModelProfile:
    id: str
    name: str
    base_url: str
    model: str
    api_key: str = ""
    api_key_env: str = ""
    vision: bool = False
    json_mode: bool = True
    temperature: float = 0.2
    max_tokens: int = 4096
    note: str = ""

    def resolved_key(self) -> str:
        """先看写进配置的密钥，再看环境变量。"""
        if self.api_key and self.api_key.strip():
            return self.api_key.strip()
        if self.api_key_env:
            return (os.environ.get(self.api_key_env) or "").strip()
        return ""

    def to_dict(self, *, with_key: bool = True) -> dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "vision": self.vision,
            "json_mode": self.json_mode,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "note": self.note,
        }
        if with_key:
            d["api_key"] = self.api_key
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ModelProfile":
        return ModelProfile(
            id=str(d.get("id") or "custom"),
            name=str(d.get("name") or d.get("id") or "自定义模型"),
            base_url=str(d.get("base_url") or "").rstrip("/"),
            model=str(d.get("model") or ""),
            api_key=str(d.get("api_key") or ""),
            api_key_env=str(d.get("api_key_env") or ""),
            vision=bool(d.get("vision", False)),
            json_mode=bool(d.get("json_mode", True)),
            temperature=float(d.get("temperature", 0.2)),
            max_tokens=int(d.get("max_tokens", 4096)),
            note=str(d.get("note") or ""),
        )


def preset_models() -> list[ModelProfile]:
    """开箱预设。默认 DeepSeek —— deepseek-flash 支持图像理解，教学模式可直接用。"""
    return [
        ModelProfile(
            id="deepseek-flash",
            name="DeepSeek Flash（默认·支持读图）",
            base_url="https://api.deepseek.com",
            model="deepseek-flash",
            api_key_env="DEEPSEEK_API_KEY",
            vision=True,
            note="DeepSeek-V4.1-Flash，支持图像理解，修图对话与教学模式都可用。",
        ),
        ModelProfile(
            id="deepseek-v4-pro",
            name="DeepSeek V4 Pro（不支持读图）",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key_env="DEEPSEEK_API_KEY",
            vision=False,
            note="更强推理，但官方明确不支持图像理解，教学模式不可用。",
        ),
        ModelProfile(
            id="qwen-vl",
            name="通义千问 VL（百炼·支持读图）",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen-vl-max",
            api_key_env="DASHSCOPE_API_KEY",
            vision=True,
            note="阿里百炼 OpenAI 兼容端点。",
        ),
        ModelProfile(
            id="glm-4v",
            name="智谱 GLM-4V（支持读图）",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model="glm-4v-plus",
            api_key_env="ZHIPU_API_KEY",
            vision=True,
        ),
        ModelProfile(
            id="siliconflow-vl",
            name="硅基流动 Qwen2.5-VL（支持读图）",
            base_url="https://api.siliconflow.cn/v1",
            model="Qwen/Qwen2.5-VL-72B-Instruct",
            api_key_env="SILICONFLOW_API_KEY",
            vision=True,
        ),
        ModelProfile(
            id="openai",
            name="OpenAI（支持读图）",
            base_url="https://api.openai.com/v1",
            model="gpt-4o",
            api_key_env="OPENAI_API_KEY",
            vision=True,
        ),
        ModelProfile(
            id="ollama",
            name="本地 Ollama（离线）",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen2.5vl:7b",
            api_key="ollama",
            api_key_env="",
            vision=True,
            note="需先在本机跑起 Ollama 并拉取视觉模型。",
        ),
        ModelProfile(
            id="custom",
            name="自定义（任何 OpenAI 兼容端点）",
            base_url="",
            model="",
            vision=True,
            note="填入 base_url 与模型名即可，例如中转/自建服务。",
        ),
    ]


# ---------------------------------------------------------------- 抠图参数

def guess_matting_dir() -> str:
    """探测已有的 BiRefNet/RMBG 权重目录。

    优先级：环境变量 RMBG_MODEL_DIR → 下面这些常见布局（ComfyUI 的默认模型目录各不相同，
    这里只是"顺手找到就用"，找不到就走 OpenCV 兜底并给出提示）。
    """
    env = os.environ.get("RMBG_MODEL_DIR", "")
    candidates = ([env] if env else []) + [
        str(Path.home() / "ComfyUI" / "models" / "RMBG" / "RMBG-2.0"),
        r"C:\ComfyUI\models\RMBG\RMBG-2.0",
        r"D:\ComfyUI\models\RMBG\RMBG-2.0",
    ]
    for c in candidates:
        p = Path(c)
        if (p / "birefnet.py").is_file() and (p / "BiRefNet_config.py").is_file():
            return str(p)
    for root in (r"C:\AI\ComfyUI\models\RMBG", r"D:\AI\ComfyUI\models\RMBG"):
        rp = Path(root)
        if rp.is_dir():
            for sub in sorted(rp.iterdir()):
                if (sub / "birefnet.py").is_file():
                    return str(sub)
    # 主目录下的第一层
    if rp.is_dir():
        return str(rp)
    return ""


# ---------------------------------------------------------------- 设置对象

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "models": [],           # 空则用 preset_models()
    "active_model": "deepseek-flash",
    "teaching_model": "",   # 空 = 跟随 active_model
    "preview": {"max_side": 1400, "jpeg_quality": 88},
    "matting": {
        "engine": "auto",     # auto|birefnet|grabcut
        "model_dir": "",      # 空则自动探测
        "resolution": 1024,
        "device": "auto",     # auto|cuda|cpu
        "feather": 1.0,
    },
    "export": {"format": "png", "jpeg_quality": 95},
    "vision": {"chat_detail": "low", "teaching_detail": "high"},
    # 教学模式一次要输出 summary + 7 项评分 + 优点 + 问题 + 建议，是最长的结构化回答；
    # 上限给足，避免被截断成非法 JSON（截断时也会自动重问一次更简短的版本）
    "teaching": {"max_tokens": 6144, "target": "original"},
    # 技能包：审美与修图领域知识（常驻准则 + 按需加载的参考）
    "skills": {
        "enabled": [],                      # 空 = 全部启用
        "disabled": [],
        "max_refs_per_turn": 2,
        "teach_auto": ["diagnose", "avoid"],   # 教学模式自动预载的参考
    },
    # 对话行为
    "chat": {
        "auto_refine": True,                # 应用完指令后再自我审查一轮并修正
        "refine_max_ops": 3,
    },
    # 网络：国内访问部分模型服务可能需要走本机代理
    "network": {
        "use_proxy": False,
        "proxy": "http://127.0.0.1:7897",
        "timeout": 180,
    },
    # RAW（CR3/CR2/NEF/ARW/DNG…）解码策略
    "raw": {
        "decode": "auto",       # auto=有 rawpy 就用真解码，否则取内嵌全尺寸预览；rawpy；preview
        "half_size": False,     # rawpy 半尺寸解码（更快，画质略降）
    },
}


@dataclass
class Settings:
    data: dict[str, Any] = field(default_factory=dict)

    # -- 载入/保存 --------------------------------------------------
    @staticmethod
    def load() -> "Settings":
        ensure_dirs()
        raw: dict[str, Any] = {}
        if SETTINGS_FILE.is_file():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        merged = copy.deepcopy(DEFAULTS)
        for k, v in (raw or {}).items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        s = Settings(merged)
        if not s.data["models"]:
            s.data["models"] = [m.to_dict() for m in preset_models()]
        if not s.data["matting"].get("model_dir"):
            s.data["matting"]["model_dir"] = guess_matting_dir()
        return s

    def save(self) -> None:
        ensure_dirs()
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)

    # -- 模型 ------------------------------------------------------
    @property
    def models(self) -> list[ModelProfile]:
        return [ModelProfile.from_dict(d) for d in self.data.get("models", [])]

    def model(self, model_id: str | None = None) -> ModelProfile:
        models = self.models
        wanted = model_id or self.data.get("active_model") or models[0].id
        for m in models:
            if m.id == wanted:
                return m
        return models[0]

    def teaching_model(self) -> ModelProfile:
        tid = (self.data.get("teaching_model") or "").strip()
        return self.model(tid) if tid else self.model()

    def upsert_model(self, prof: ModelProfile) -> None:
        models = [m.to_dict() for m in self.models]
        for i, m in enumerate(models):
            if m.get("id") == prof.id:
                models[i] = prof.to_dict()
                break
        else:
            models.append(prof.to_dict())
        self.data["models"] = models

    def remove_model(self, model_id: str) -> bool:
        models = [m.to_dict() for m in self.models if m.id != model_id]
        if len(models) == len(self.models):
            return False
        self.data["models"] = models
        if self.data.get("active_model") == model_id and models:
            self.data["active_model"] = models[0]["id"]
        return True

    # -- 便捷读取 --------------------------------------------------
    @property
    def preview_max_side(self) -> int:
        return int(self.data["preview"]["max_side"])

    @property
    def preview_quality(self) -> int:
        return int(self.data["preview"]["jpeg_quality"])

    @property
    def matting(self) -> dict[str, Any]:
        return self.data["matting"]

    # -- 技能包 / 对话 / 网络 --------------------------------------
    @property
    def skills(self) -> dict[str, Any]:
        return self.data["skills"]

    @property
    def chat(self) -> dict[str, Any]:
        return self.data["chat"]

    @property
    def network(self) -> dict[str, Any]:
        return self.data["network"]

    @property
    def raw(self) -> dict[str, Any]:
        return self.data["raw"]

    def proxy_url(self) -> str | None:
        """按设置返回代理地址；未启用返回 None。"""
        net = self.network
        if not net.get("use_proxy"):
            return None
        url = str(net.get("proxy") or "").strip()
        return url or None

    def public(self) -> dict[str, Any]:
        """给前端/日志用：不含任何密钥。"""
        return {
            "version": self.data.get("version", 1),
            "models": [m.to_dict(with_key=False) for m in self.models],
            "models_with_key": [m.id for m in self.models if m.resolved_key()],
            "has_env_key": {
                m.id: bool(m.api_key_env and os.environ.get(m.api_key_env))
                for m in self.models
            },
            "active_model": self.data.get("active_model"),
            "teaching_model": self.data.get("teaching_model", ""),
            "matting": self.matting,
            "preview": self.data["preview"],
            "export": self.data["export"],
            "skills": self.skills,
            "chat": self.chat,
            "teaching": {k: v for k, v in (self.data.get("teaching") or {}).items()},
            "network": {k: v for k, v in self.network.items()},
            "raw": {k: v for k, v in self.raw.items()},
            "settings_file": str(SETTINGS_FILE),
        }
