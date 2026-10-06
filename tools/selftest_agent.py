"""对话代理自检：用假模型驱动整条链路，无需 API 密钥即可验证
  · 诊断 / 意图字段是否被解析并记录
  · "load" 是否真的触发按需加载，且参考正文真的进了下一次请求
  · 自动精修（复核轮）是否真的发出、批准与执行
  · 非法 JSON / 非法算子是否被回灌纠错
  · 文本模型（无读图能力）是否改用客观指标而不是假装看图
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from cogitator import agent as agent_mod  # noqa: E402
from cogitator import llm, skills  # noqa: E402
from cogitator.config import Settings  # noqa: E402
from cogitator.document import Project  # noqa: E402

FAIL: list[str] = []
TMP = Path(tempfile.mkdtemp(prefix="cogitator_agent_"))


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


class FakeClient:
    """按剧本回话的假客户端；记录每次请求的 messages 供断言。"""

    def __init__(self, script: list[str], finish: list[str] | None = None) -> None:
        self.script = list(script)
        self.finish = list(finish or [])
        self.calls: list[list[dict]] = []

    def check_ready(self, need_vision: bool = False) -> None:
        return None

    def chat(self, messages, json_mode=False, temperature=None, max_tokens=None, retries=2):
        # 必须深拷贝：agent 会在同一轮里继续 append（加载参考、回灌纠错），
        # 存引用会让"第一轮没有参考正文"这类断言永远成立而失去意义。
        import copy as _copy

        self.calls.append(_copy.deepcopy(messages))
        text = self.script.pop(0) if self.script else '{"reply":"（兜底）","ops":[]}'
        fr = self.finish.pop(0) if self.finish else "stop"
        return llm.ChatResult(text=text, model="fake-model",
                              usage={"total_tokens": 7}, elapsed=0.05, finish_reason=fr)

    def last_text_blob(self) -> str:
        return self.blob(len(self.calls) - 1)

    def blob(self, i: int) -> str:
        """第 i 次调用里所有文本块（图片记为 <image>）。"""
        out = []
        for m in self.calls[i]:
            c = m.get("content")
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                for b in c:
                    out.append(b.get("text", "") if b.get("type") == "text" else "<image>")
        return "\n".join(out)


def mkpng(path: Path, arr: np.ndarray) -> str:
    Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), mode="RGB").save(path)
    return str(path)


def make_project(settings, name: str) -> Project:
    a = np.zeros((64, 64, 3), np.float32)
    a[..., 0] = 0.35
    a[..., 1] = 0.45
    a[..., 2] = 0.6
    a[16:48, 16:48] = (0.9, 0.7, 0.3)
    return Project.create_from_images([mkpng(TMP / f"{name}.png", a)], settings=settings, name=name)


def patch_client(fake: FakeClient):
    """把 agent 用的 LLMClient 换成假客户端。"""
    real = llm.LLMClient
    agent_mod.llm.LLMClient = lambda *a, **k: fake          # type: ignore[assignment]
    return real


def main() -> int:
    import cogitator.document as docmod
    docmod.PROJECTS_DIR = TMP / "projects"
    docmod.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    settings = Settings.load()
    settings.data["chat"]["auto_refine"] = True
    settings.data["chat"]["refine_max_ops"] = 3
    settings.data["skills"]["max_refs_per_turn"] = 2
    real_client = llm.LLMClient

    try:
        print("== 按需加载参考（渐进式披露）==")
        proj = make_project(settings, "加载测试")
        fake = FakeClient([
            json.dumps({"diagnosis": "整体偏暗，背景偏蓝。", "intent": "提亮并中和色偏",
                        "reply": "先取参考。", "ops": [], "load": ["composition", "color"],
                        "question": ""}, ensure_ascii=False),
            json.dumps({"diagnosis": "欠曝约 0.2EV，白平衡偏冷。", "intent": "让主体透亮、背景中性",
                        "reply": "已提亮 0.2EV 并加暖。", "ops": [{"op": "exposure", "value": 0.2}],
                        "load": [], "question": ""}, ensure_ascii=False),
        ])
        patch_client(fake)
        rep = agent_mod.ImageAgent(proj, settings).ask("这张有点暗有点蓝", refine=False)
        check(len(fake.calls) == 2, f"load 触发第二轮请求（共 {len(fake.calls)} 次调用）")
        blob = fake.blob(1)
        check("参考资料：构图与视觉重量" in blob and "参考资料：色彩与白平衡" in blob,
              "参考正文真的注入了第二次请求")
        check("五条铁律" in blob, "常驻准则也在提示里")
        check("jp-airy" in blob and "风格配方" in blob, "风格配方清单已注入")
        check("参考资料" not in fake.blob(0), "第一轮请求里没有参考正文（渐进式披露）")
        check(rep.loaded == ["composition", "color"], f"记录已加载参考：{rep.loaded}")
        check(rep.loaded_titles and "构图" in rep.loaded_titles[0], f"记录参考标题：{rep.loaded_titles}")
        check(rep.diagnosis and rep.intent, f"解析出诊断/意图：{rep.diagnosis[:18]}… / {rep.intent[:18]}…")
        check([o["op"] for o in rep.ops] == ["exposure"], "指令已执行")
        st = proj.state()["layers"][0]
        check(st["op_count"] >= 1, f"文档里真的多了步骤（{st['op_count']} 步）")

        print("\n== 自动精修（复核轮）==")
        proj2 = make_project(settings, "精修测试")
        fake2 = FakeClient([
            json.dumps({"diagnosis": "略暗。", "intent": "提亮", "reply": "提亮 0.3EV。",
                        "ops": [{"op": "exposure", "value": 0.3}], "load": [], "question": ""},
                        ensure_ascii=False),
            json.dumps({"verdict": "部分达成", "problems": ["右下角仍偏暗"],
                        "ops": [{"op": "shadows", "value": 12}]}, ensure_ascii=False),
        ])
        patch_client(fake2)
        rep2 = agent_mod.ImageAgent(proj2, settings).ask("提亮一点")
        check(len(fake2.calls) == 2, f"发出了复核轮（共 {len(fake2.calls)} 次调用）")
        check("复核" in fake2.calls[1][0]["content"], "复核轮用的是复核提示词")
        check(rep2.refine.get("verdict") == "部分达成", f"复核结论已记录：{rep2.refine.get('verdict')}")
        check(rep2.refine.get("problems") == ["右下角仍偏暗"], "复核问题已记录")
        check([o["op"] for o in rep2.refine.get("ops", [])] == ["shadows"], "复核修正已执行")
        cnt = proj2.state()["layers"][0]["op_count"]
        check(cnt == 2, f"两轮指令都落在文档里（共 {cnt} 步）")

        print("\n== 复核允许回退 ==")
        proj3 = make_project(settings, "回退测试")
        fake3 = FakeClient([
            json.dumps({"diagnosis": "偏冷。", "intent": "加暖", "reply": "加暖。",
                        "ops": [{"op": "temperature", "value": 20}], "load": [], "question": ""},
                       ensure_ascii=False),
            json.dumps({"verdict": "未达成", "problems": ["肤色过黄"],
                        "ops": [{"op": "remove_op", "index": -1},
                                {"op": "temperature", "value": 8}]}, ensure_ascii=False),
        ])
        patch_client(fake3)
        agent_mod.ImageAgent(proj3, settings).ask("调暖一点")
        ops_now = [o["op"] for o in proj3.layer("L1")["ops"]]
        check(ops_now == ["temperature"], f"复核撤回了过头的步骤并重做（现为 {ops_now}）")
        check(abs(float(proj3.layer("L1")["ops"][0]["value"]) - 8) < 1e-6, "重做的幅度是复核给的 8")

        print("\n== 纠错：非法 JSON 与非法算子 ==")
        proj4 = make_project(settings, "纠错测试")
        fake4 = FakeClient([
            "这不是 JSON",
            json.dumps({"diagnosis": "x", "intent": "y", "reply": "z",
                        "ops": [{"op": "不存在的算子"}], "load": [], "question": ""},
                       ensure_ascii=False),
            json.dumps({"diagnosis": "欠曝。", "intent": "提亮", "reply": "已提亮 0.2EV。",
                        "ops": [{"op": "exposure", "value": 0.2}], "load": [], "question": ""},
                       ensure_ascii=False),
        ])
        patch_client(fake4)
        rep4 = agent_mod.ImageAgent(proj4, settings).ask("提亮", refine=False)
        check(len(fake4.calls) == 3, f"两次纠错后成功（共 {len(fake4.calls)} 次调用）")
        check(any("JSON" in str(m.get("content")) for m in fake4.calls[1]) or
              any("校验" in str(m.get("content")) for m in fake4.calls[2]),
              "错误被回灌给模型")
        check([o["op"] for o in rep4.ops] == ["exposure"] and not rep4.error,
              "最终拿到了可执行指令且无错误")
        check(proj4.state()["layers"][0]["op_count"] == 1, "只有一次有效落地（失败的批次没污染文档）")

        print("\n== 关闭自动精修 ==")
        proj5 = make_project(settings, "关精修")
        fake5 = FakeClient([
            json.dumps({"diagnosis": "好。", "intent": "轻微", "reply": "微调。",
                        "ops": [{"op": "contrast", "value": 5}], "load": [], "question": ""},
                       ensure_ascii=False),
        ])
        patch_client(fake5)
        rep5 = agent_mod.ImageAgent(proj5, settings).ask("稍微加点对比", refine=False)
        check(len(fake5.calls) == 1 and not rep5.refine, "refine=False 时只调用一次、无复核")

        print("\n== 文本模型（无读图能力）走客观指标 ==")
        settings.data["active_model"] = "deepseek-v4-pro"      # 预设里 vision=False
        proj6 = make_project(settings, "文本模型")
        fake6 = FakeClient([
            json.dumps({"diagnosis": "据指标偏暗。", "intent": "提亮", "reply": "提亮。",
                        "ops": [{"op": "exposure", "value": 0.15}], "load": [], "question": ""},
                       ensure_ascii=False),
        ])
        patch_client(fake6)
        rep6 = agent_mod.ImageAgent(proj6, settings).ask("提亮")
        blob6 = fake6.last_text_blob()
        check("客观指标" in blob6 and "平均亮度" in blob6, "提示里带上了客观图像指标")
        check("<image>" not in blob6, "没有发送图片（该模型不支持读图）")
        check(rep6.vision_used is False, "标记为未使用视觉")
        settings.data["active_model"] = "deepseek-flash"

        print("\n== 只提问不动手 ==")
        proj7 = make_project(settings, "只提问")
        n_before = proj7.state()["layers"][0]["op_count"]
        fake7 = FakeClient([
            json.dumps({"diagnosis": "曝光准确、构图平衡。", "intent": "无",
                        "reply": "这张已经很平衡，建议保持；若要更通透可 +0.1EV。",
                        "ops": [], "load": [], "question": "要我试着加 0.1EV 看看吗？"},
                       ensure_ascii=False),
        ])
        patch_client(fake7)
        rep7 = agent_mod.ImageAgent(proj7, settings).ask("这张还能怎么修")
        check(not rep7.ops and not rep7.applied, "ops 为空时不动手")
        check(rep7.question.startswith("要我"), f"提出具体问题：{rep7.question}")
        check(proj7.state()["layers"][0]["op_count"] == n_before, "文档没有任何改变")

        print("\n== 对话记录 ==")
        chat = proj7.state()["chat"]
        check(len(chat) >= 2 and chat[0]["role"] == "user", f"对话已记录（{len(chat)} 条）")
        a = [m for m in chat if m["role"] == "assistant"]
        check(a and a[-1].get("diagnosis") and a[-1].get("intent"), "助手消息里存了诊断与意图")
        check(a and a[-1].get("question"), "助手消息里存了提问")
        check((proj7.dir / "chat.json").is_file(), "对话已落盘")
        chat1 = proj.state()["chat"]
        a1 = [m for m in chat1 if m["role"] == "assistant"]
        check(a1 and a1[-1].get("loaded"), f"助手消息里记下了加载过的参考：{a1[-1].get('loaded') if a1 else None}")
        a2 = [m for m in proj2.state()["chat"] if m["role"] == "assistant"][-1]
        check(a2.get("refine", {}).get("applied"), "助手消息里记下了精修结果")

        # ------------------------------------------------------------------
        print("\n== 教学模式的 JSON 容错（用户报「返回 json 格式不对」）==")
        from cogitator import teaching as teach_mod

        T_JSON = ('{"summary":"构图稳健，天空过曝约一档。",'
                  '"scores":{"exposure":6,"composition":7,"lighting":6},'
                  '"strengths":["主体清晰","水平线校准到位"],'
                  '"issues":[{"title":"天空过曝","detail":"画面上部约 1/4 贴白","fix":"降曝光补偿 -1EV"}],'
                  '"shooting_tips":["测光点移到天空","开启高光警告"],"load":[]}')
        projT = make_project(settings, "教学JSON")

        fakeT = FakeClient(["好的，我来点评：\n```json\n" + T_JSON + "\n```\n希望有帮助。"])
        patch_client(fakeT)
        c1 = teach_mod.TeachingSession(projT, settings).critique("original")
        check(bool(c1.summary and c1.scores), f"围栏+前后废话能解析（{c1.summary[:14]}…）")
        check(len(fakeT.calls) == 1, "正常返回只需一次调用（不做多余重问）")

        # 旧的 rfind("}") 解析器会在"后记里含花括号"时切错 —— 真实模型常这么写
        fakeT2 = FakeClient([T_JSON + "\n\n（评分 1-10，未评项用 {} 表示）"])
        patch_client(fakeT2)
        c2 = teach_mod.TeachingSession(projT, settings).critique("original")
        check(bool(c2.scores), "JSON 后面跟着含花括号的后记，仍能解析")

        # 第一次完全不是 JSON → 自动重问一次
        fakeT3 = FakeClient(["抱歉，我无法给出结构化点评。", T_JSON])
        patch_client(fakeT3)
        c3 = teach_mod.TeachingSession(projT, settings).critique("original")
        check(bool(c3.scores) and len(fakeT3.calls) == 2,
              f"第一次非 JSON → 自动重问一次并成功（调用 {len(fakeT3.calls)} 次）")
        check(c3.retried and "重问" in c3.parse_note, f"如实记录了重问：{c3.parse_note}")
        check("只输出一个合法的 JSON" in fakeT3.last_text_blob(),
              "重问时明确要求只输出合法 JSON")

        # 被长度上限截断 → 救回已完整写好的字段并如实说明
        cut = T_JSON[:T_JSON.index('"issues"') + 40]
        fakeT4 = FakeClient([cut], finish=["length"])
        patch_client(fakeT4)
        c4 = teach_mod.TeachingSession(projT, settings).critique("original")
        check(bool(c4.scores) and bool(c4.summary), "被截断的返回仍救回 summary 与 scores")
        check("截断" in c4.parse_note, f"如实说明被截断：{c4.parse_note}")

        # 截断且重问也失败 → 错误信息必须点明"截断"，而不是含糊的"格式不对"
        fakeT5 = FakeClient(['{"a":,,"b":}', "还是不行"], finish=["length", "stop"])
        patch_client(fakeT5)
        try:
            teach_mod.TeachingSession(projT, settings).critique("original")
            check(False, "两次都坏时应报错")
        except llm.LLMError as e:
            msg = str(e)
            check("截断" in msg or "JSON" in msg, f"错误信息可定位：{msg[:60]}")
            check("格式不对" not in msg, "不再使用含糊的「格式不对」措辞")

        print("\n== 教学到底看哪张图（原图 / 当前成图）==")
        import cogitator.document as _dm

        projS = make_project(settings, "教学看图")
        sessS = teach_mod.TeachingSession(projS, settings)
        src_orig = sessS._source_image("original")
        src_cur = sessS._source_image("current")
        layer_src = projS.dir / projS.doc["layers"][0]["source"]
        check(src_orig.resolve() == layer_src.resolve(),
              f"target=original 送的是图层源文件（{src_orig.name}），不是画布合成")
        check(src_cur.resolve() == projS.preview_path().resolve(),
              f"target=current 送的是画布预览（{src_cur.name}）")
        projS.apply_ops([{"op": "exposure", "value": 0.8, "layer": "L1"}])
        check(sessS._source_image("original").resolve() == layer_src.resolve(),
              "做过调色后，original 仍然是未修的源文件（不带任何修改）")
        check(sessS._source_image("current").resolve() == projS.preview_path().resolve(),
              "而 current 是修过之后的成图")

        fakeS = FakeClient([T_JSON])
        patch_client(fakeS)
        cS = teach_mod.TeachingSession(projS, settings).critique("current")
        check(cS.target == "current", f"critique('current') 记录 target={cS.target}")
        blobS = fakeS.last_text_blob()
        check("已经修过的成图" in blobS, "送给模型的说明写明「这是用户已经修过的成图」")
        fakeS2 = FakeClient([T_JSON])
        patch_client(fakeS2)
        cO = teach_mod.TeachingSession(projS, settings).critique("original")
        check(cO.target == "original" and "原始照片" in fakeS2.last_text_blob(),
              "critique('original') 说明写明「这是原始照片」")
        js = (Path(__file__).resolve().parent.parent / "cogitator" / "web" / "app.js").read_text(encoding="utf-8")
        check("target: 'original'" not in js and "teachTarget()" in js,
              "界面不再写死 target:'original'，改为按用户选择（teachTarget）")
        check("teaching" in json.dumps(settings.public(), ensure_ascii=False),
              "设置里暴露 teaching（供界面读写看图选择）")
    finally:
        llm.LLMClient = real_client                       # type: ignore[assignment]
        agent_mod.llm.LLMClient = real_client             # type: ignore[assignment]

    print()
    if FAIL:
        print(f"对话代理自检失败 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("对话代理自检全部通过。")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(code)
