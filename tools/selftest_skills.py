"""技能包与风格配方的自检：解析、索引、按需加载、注册表校验。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogitator import ops, skills, styles  # noqa: E402

FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def main() -> int:
    print("== 技能包发现 ==")
    packs = skills.discover()
    check(len(packs) >= 1, f"发现 {len(packs)} 个技能包：{[p.id for p in packs]}")
    if not packs:
        return 1
    p = packs[0]
    check(bool(p.name) and bool(p.description), f"frontmatter 解析：{p.name} / {p.description}")
    check(len(p.core) > 400, f"常驻准则正文 {len(p.core)} 字")
    check(len(p.refs) == 11, f"发现 {len(p.refs)} 份参考")
    ids = [r.id for r in p.refs]
    check(len(set(ids)) == len(ids), "参考 id 无重复")
    check(all(r.title and r.summary for r in p.refs),
          "每份参考都有标题与摘要")
    idx_ids = {line.split("|")[0].replace("- ", "").strip()
               for line in p.core.splitlines() if "|" in line and line.strip().startswith("-")}
    check(set(ids) <= idx_ids, f"索引与文件一致（缺 {sorted(set(ids) - idx_ids) or '无'}）")
    for r in p.refs:
        check(r.path.is_file() and r.path.stat().st_size > 500,
              f"参考 {r.id} 有内容（{r.path.stat().st_size} 字节）")

    print("\n== 提示词注入 ==")
    core = skills.core_prompt()
    check("五条铁律" in core and "肤色是红线" in core, "常驻准则含铁律")
    idx = skills.index_prompt()
    check(idx.count("\n") >= 10 and "load" in idx, f"索引列出 {idx.count(chr(10))} 行且提示如何加载")
    total_core = len(core) + len(idx)
    check(total_core < 4200, f"常驻注入体积可控：{total_core} 字（不含参考正文）")

    print("\n== 按需加载 ==")
    text, loaded, missing = skills.load_texts(["composition", "color"])
    check(len(loaded) == 2 and not missing, f"按 id 加载：{loaded}")
    check("视觉重量" in text and "肤色红线" in text, "加载到的正文内容正确")
    t2, l2, m2 = skills.load_texts(["构图与视觉重量"])
    check(len(l2) == 1, f"按中文标题也能解析：{l2}")
    t3, l3, m3 = skills.load_texts(["composition.md"])
    check(len(l3) == 1, f"按文件名也能解析：{l3}")
    t4, l4, m4 = skills.load_texts(["不存在的东西"])
    check(not l4 and m4, f"未知名字进入 missing：{m4}")
    t5, l5, _ = skills.load_texts(["composition", "color", "light", "avoid"])
    check(len(l5) <= 2, f"一次最多加载 2 份（实得 {len(l5)}）")
    check(skills.status()["reference_count"] == 11, "状态接口统计参考数")

    print("\n== 风格配方校验（逐条过算子注册表）==")
    errs = styles.self_check()
    check(not errs, f"13 套配方全部合法（错误 {len(errs)} 条：{errs[:3]}）")
    check(len(styles.load_styles()) >= 12, f"配方数量 {len(styles.load_styles())}")
    groups = {s.group for s in styles.load_styles()}
    check(len(groups) >= 4, f"覆盖多个类别：{sorted(groups)}")

    print("\n== 强度缩放 ==")
    base = styles.get_style("film-warm")
    s100 = styles.scale_ops(base.ops, 100)
    s50 = styles.scale_ops(base.ops, 50)
    s150 = styles.scale_ops(base.ops, 150)
    b = next(o for o in s100 if o["op"] == "grain")["amount"]
    h = next(o for o in s50 if o["op"] == "grain")["amount"]
    u = next(o for o in s150 if o["op"] == "grain")["amount"]
    check(h < b < u, f"强度缩放生效：50%={h} 100%={b} 150%={u}")
    st = next(o for o in s50 if o["op"] == "split_tone")
    check(st["highlight_hue"] == 45, "色相角度不被缩放（保持 45）")
    check(st["highlight_sat"] < 18, f"着色强度被缩放（{st['highlight_sat']} < 18）")
    check(all(isinstance(v, int) for o in s150 for k, v in o.items()
              if k != "op" and isinstance(v, int)), "整数参数缩放后仍是整数")

    print("\n== 套用风格展开 ==")
    style, expanded = styles.expand("jp-airy", 80)
    check(style.name == "日系空气感", f"解析到风格：{style.name}")
    check(len(expanded) == len(style.ops), f"展开 {len(expanded)} 条算子")
    check(all(o.get("layer") is None for o in expanded), "展开结果都指向当前图层")
    try:
        styles.expand("不存在的风格")
        check(False, "未知风格应报错")
    except KeyError:
        check(True, "未知风格报 KeyError（文档层会转成可读提示）")

    print("\n== 模型可见的风格清单 ==")
    m = styles.manual()
    check(all(s.id in m for s in styles.load_styles()), "清单覆盖全部配方")
    check("强度" in m and "起点" in m, "清单含使用纪律")

    print()
    if FAIL:
        print(f"技能包自检失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("技能包与风格配方自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
