"""教学模式 JSON 容错体检：用真实模型常见的畸形输出逐条试解析。

用户报告教学「容易报错：返回 json 格式不对」。修之前先逐条测出**哪一种**输出会挂，
修之后用同一张表钉住，避免再退化。
预期分三类：
  parse            能解析出对象
  parse_truncated  被截断但能补齐（要求 truncated=True，且尽量保住已经完整写好的字段）
  error            实在不可解析时，必须给出**可读且准确**的错误（而不是"格式不对"这种糊话）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cogitator import llm  # noqa: E402

GOOD = ('{"summary":"不错的街拍","scores":{"composition":7,"light":6},'
        '"strengths":["构图稳"],"issues":[{"what":"天空过曝","where":"画面右上",'
        '"how_to_shoot":"降低曝光补偿 -1EV"}]}')

TRUNCATED = ('{"summary":"这张照片整体不错，构图稳健，曝光基本准确，天空略有过曝。",'
             '"scores":{"composition":7,"light":6,"color":7,"focus":8,"exposure":6,'
             '"story":7,"impact":6},"strengths":["构图稳健","主体清晰"],'
             '"issues":[{"what":"天空过曝","where":"画面上部","how_to_shoot":"降低曝光补偿"},'
             '{"what":"暗部略脏","where":"左下角","how_to_shoot":"提高ISO"')

SAMPLES: list[tuple[str, str, str, list[str]]] = [
    ("裸 JSON", GOOD, "parse", ["summary", "scores", "strengths", "issues"]),
    ("```json 围栏", "```json\n" + GOOD + "\n```", "parse", ["summary", "issues"]),
    ("围栏无语言标记", "```\n" + GOOD + "\n```", "parse", ["summary"]),
    ("前言 + 围栏", "好的，我来点评这张照片。\n\n```json\n" + GOOD + "\n```\n希望对你有帮助。", "parse", ["summary", "scores"]),
    ("前言 + 裸 JSON", "以下是我的点评：\n" + GOOD, "parse", ["summary"]),
    ("尾随后记（含花括号）", GOOD + "\n\n（评分 1-10，未评项用 {} 表示）", "parse", ["summary", "issues"]),
    ("JSON 内含 // 注释", '{"summary":"好照片", // 这是注释\n "scores":{"composition":7}}', "parse", ["summary", "scores"]),
    ("尾随逗号", '{"summary":"好照片","strengths":["构图稳",],}', "parse", ["summary", "strengths"]),
    ("单引号", "{'summary':'好照片','scores':{'composition':7}}", "parse", ["summary", "scores"]),
    ("中文全角引号与冒号", '｛"summary"："好照片"，"scores"：｛"composition"：7｝｝', "parse", ["summary", "scores"]),
    ("键未加引号", '{summary:"好照片",scores:{composition:7}}', "parse", ["summary", "scores"]),
    ("字符串里含右花括号 + 后记",
     '{"summary":"画面中的 } 符号很有味道","scores":{"composition":7}}\n\n备注：{} 表示未评分', "parse", ["summary", "scores"]),
    ("两个围栏（示例 + 答案）",
     "示例：\n```json\n{\"summary\":\"示例\"}\n```\n答案：\n```json\n" + GOOD + "\n```", "parse", ["summary", "scores", "strengths"]),
    # 截断：要求"已经完整写好的字段必须保住"，而不是硬要求某个字段存在
    ("被长度截断（少了收尾括号）", TRUNCATED, "parse_truncated", ["summary", "scores", "strengths"]),
    ("被截断在最外层键处", '{"summary":"好的","scores":{"composition":7},"issues":', "parse_truncated", ["summary", "scores"]),
    ("纯文字拒绝（无 JSON）", "抱歉，我无法点评这张照片。", "error", []),
    ("结构坏了且无从补救", '{"a":,,"b":}', "error", []),
]


def main() -> int:
    ok = bad = 0
    for name, text, expect, required in SAMPLES:
        verdict_detail = ""
        try:
            res = llm.parse_json_reply_ex(text)
            got = "parse_truncated" if res.truncated else "parse"
            keys = list(res.data)[:4] if isinstance(res.data, dict) else type(res.data).__name__
            verdict_detail = ("解析成功" if not res.truncated else "截断已补齐") + f"（{keys}）"
            if res.repaired:
                verdict_detail += " 修复=" + "+".join(res.repaired)
            missing = [k for k in required if not isinstance(res.data, dict) or k not in res.data]
            if missing:
                got = "missing"
                verdict_detail += f" 丢了字段={missing}"
            elif expect == "parse_truncated" and not res.truncated:
                got = "parse"
        except llm.LLMError as e:
            got = "error"
            msg = str(e)
            verdict_detail = f"报错：{msg[:56]}"
            if expect == "error" and ("截断" in msg or "可解析" in msg):
                verdict_detail += " ← 信息可读"
            elif expect == "error":
                got = "parse"
        good = got == expect
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
        print(f"  {'ok  ' if good else 'FAIL'} {name:22} 期望={expect:16} → {verdict_detail}")
    print(f"\nJSON 容错体检：符合预期 {ok} 项，不符合 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
