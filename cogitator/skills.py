"""技能包（skill pack）：给模型注入审美与修图的领域知识。

结构（人可读可编辑）：
    skills/<包名>/SKILL.md          —— frontmatter + 正文（正文=常驻准则，含参考索引）
    skills/<包名>/references/*.md   —— 大部头参考，按需加载

渐进式披露：系统提示里只放「常驻准则 + 参考索引」（几百字），细节留在 references/。
模型可以在返回的 JSON 里用 "load": ["composition","color"] 请求加载；
本模块负责解析请求、取出正文、注入后重问一轮 —— 因此提示词永远不会被撑爆，
而模型仍然拿得到它真正需要的那份知识。

参考文件的 frontmatter 也可自带 title / summary，缺省从正文首行推断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .paths import APP_ROOT

SKILLS_DIR = APP_ROOT / "skills"
FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
REF_LINE_RE = re.compile(r"^[-*]\s*(.+)$")
# 参考索引行的 id 必须是 ASCII 标识符（中文项目符号不会被误认成参考）
REF_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")

_CACHE: list["Pack"] | None = None


@dataclass
class Reference:
    id: str            # 文件主干名，如 composition
    file: str          # 相对包目录，如 references/composition.md
    title: str
    summary: str
    path: Path

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "summary": self.summary, "file": self.file}


@dataclass
class Pack:
    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    core: str = ""
    refs: list[Reference] = field(default_factory=list)
    path: Path = Path()

    def to_dict(self, with_text: bool = False) -> dict:
        d = {
            "id": self.id, "name": self.name, "description": self.description,
            "tags": list(self.tags), "references": [r.to_dict() for r in self.refs],
            "core_chars": len(self.core),
        }
        if with_text:
            d["core"] = self.core
        return d


def _parse_front(text: str) -> tuple[dict[str, Any], str]:
    m = FRONT_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"').strip("'")
        meta[k.strip()] = v
    return meta, m.group(2)


def _parse_ref_index(core: str) -> list[tuple[str, str, str]]:
    """从正文里解析参考索引行：`- id | 标题 | 一句话说明`（id 必须像 ASCII 标识符）。"""
    out: list[tuple[str, str, str]] = []
    for line in core.splitlines():
        m = REF_LINE_RE.match(line.strip())
        if not m:
            continue
        parts = [p.strip() for p in m.group(1).split("|")]
        if len(parts) < 2:
            continue
        rid = parts[0].strip("`")
        if not REF_ID_RE.match(rid):
            continue
        out.append((rid, parts[1], parts[2] if len(parts) > 2 else ""))
    return out


def _first_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:80]
    return ""


def discover(force: bool = False) -> list[Pack]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    packs: list[Pack] = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            skill_file = d / "SKILL.md"
            if not skill_file.is_file():
                continue
            raw = skill_file.read_text(encoding="utf-8")
            meta, core = _parse_front(raw)
            pack = Pack(
                id=d.name,
                name=str(meta.get("name") or d.name),
                description=str(meta.get("description") or ""),
                tags=[t.strip() for t in str(meta.get("tags") or "").split(",") if t.strip()],
                core=core.strip(),
                path=d,
            )
            declared = {rid: (title, summary) for rid, title, summary in _parse_ref_index(core)}
            ref_dir = d / "references"
            if ref_dir.is_dir():
                for f in sorted(ref_dir.glob("*.md")):
                    rid = f.stem
                    title, summary = declared.get(rid, ("", ""))
                    head = f.read_text(encoding="utf-8")[:800]
                    rmeta, _ = _parse_front(head if head.startswith("---") else "")
                    title = str(rmeta.get("title") or title or _first_line(head) or rid)
                    summary = str(rmeta.get("summary") or summary or "")
                    pack.refs.append(Reference(
                        id=rid, file=str(f.relative_to(d)).replace("\\", "/"),
                        title=title, summary=summary, path=f))
            packs.append(pack)
    _CACHE = packs
    return packs


_CACHE: list[Pack] | None = None


def reload() -> list[Pack]:
    return discover(force=True)


def get_pack(pack_id: str) -> Pack | None:
    for p in discover():
        if p.id == pack_id:
            return p
    return None


def enabled_packs(settings: Any = None) -> list[Pack]:
    packs = discover()
    if settings is None:
        return packs
    cfg = (settings.data.get("skills") or {}) if hasattr(settings, "data") else {}
    only = cfg.get("enabled") or []
    disabled = set(cfg.get("disabled") or [])
    out = [p for p in packs if p.id not in disabled]
    if only:
        out = [p for p in out if p.id in only]
    return out


def all_refs(settings: Any = None) -> list[tuple[Pack, Reference]]:
    return [(p, r) for p in enabled_packs(settings) for r in p.refs]


def resolve(names: Iterable[str], settings: Any = None) -> list[tuple[Pack, Reference]]:
    """按 id / 标题 / 文件名解析参考请求，容忍大小写与常见变体。"""
    pairs = all_refs(settings)
    out: list[tuple[Pack, Reference]] = []
    for raw in names or []:
        want = str(raw).strip().strip("`").lower()
        if not want:
            continue
        want = want.removesuffix(".md")
        for p, r in pairs:
            if want in (r.id.lower(), r.title.lower(), r.file.lower(),
                        r.file.lower().removesuffix(".md")) \
                    or want in r.title.lower() or r.id.lower() in want:
                if (p, r) not in out:
                    out.append((p, r))
                break
    return out


def core_prompt(settings: Any = None) -> str:
    parts: list[str] = []
    for p in enabled_packs(settings):
        if p.core:
            parts.append(f"# 技能包：{p.name}\n{p.core}")
    return "\n\n".join(parts)


def index_prompt(settings: Any = None, exclude: Iterable[str] = ()) -> str:
    """参考索引：只给标题与一句话，正文按需加载。"""
    done = {str(x).lower() for x in exclude}
    lines: list[str] = []
    for p in enabled_packs(settings):
        for r in p.refs:
            if r.id.lower() in done:
                continue
            lines.append(f"- {r.id}｜{r.title}：{r.summary}")
    if not lines:
        return ""
    return ("【可加载的参考（需要细节时在返回的 JSON 里加 \"load\": [\"id\", ...]，"
            "一次最多 2 个；能用常识判断时不要加载）】\n" + "\n".join(lines))


def load_refs(names: Iterable[str], settings: Any = None, limit: int = 2,
              per_ref_chars: int = 6000) -> tuple[str, list[dict], list[str]]:
    """返回 (拼接正文, [{"id","title"}...], 未找到的名字)。"""
    pairs = resolve(names, settings)
    loaded: list[dict] = []
    chunks: list[str] = []
    missing: list[str] = []
    wanted = [str(n) for n in (names or [])]
    for p, r in pairs[:limit]:
        try:
            text = r.path.read_text(encoding="utf-8")
        except OSError:
            missing.append(r.id)
            continue
        _, body = _parse_front(text)
        chunks.append(f"# 参考资料：{r.title}\n{body.strip()[:per_ref_chars]}")
        loaded.append({"id": r.id, "title": r.title, "pack": p.id})
    hit = {d["id"].lower() for d in loaded} | {d["title"].lower() for d in loaded}
    for w in wanted:
        if w.strip().removesuffix(".md").lower() not in hit:
            missing.append(w)
    return "\n\n".join(chunks), loaded, missing


def load_texts(names: Iterable[str], settings: Any = None, limit: int = 2,
               per_ref_chars: int = 6000) -> tuple[str, list[str], list[str]]:
    """兼容旧签名：返回 (拼接正文, 已加载标题, 未找到的名字)。"""
    text, loaded, missing = load_refs(names, settings, limit, per_ref_chars)
    return text, [d["title"] for d in loaded], missing


def status(settings: Any = None) -> dict:
    packs = discover()
    enabled = {p.id for p in enabled_packs(settings)}
    return {
        "dir": str(SKILLS_DIR),
        "packs": [
            {**p.to_dict(), "enabled": p.id in enabled,
             "core_chars": len(p.core),
             "reference_chars": sum(r.path.stat().st_size for r in p.refs if r.path.is_file())
             if p.refs else 0}
            for p in packs
        ],
        "reference_count": sum(len(p.refs) for p in packs),
        "enabled_count": len(enabled),
    }
