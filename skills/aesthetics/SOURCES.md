# 审美知识库的来源与边界

本目录（`skills/aesthetics/`）是给模型看的**审美工作准则**：常驻核心 `SKILL.md` +
`references/` 下 11 份按需加载的参考（构图、光影、色彩、人像、风光、街拍、静物、风格、诊断、失误清单、工作流）。

## 编写方式

正文**由本项目自行撰写**：把公开资料里的通用摄影/后期原则，整理成"诊断顺序 + 幅度纪律 + 常见失误"
这类可直接驱动决策的条目，并与本项目的 48 个算子一一对应（哪条原则对应哪几个算子、参数该给多大）。
不复制原文段落，只吸收观点。

## 参考来源

以下是编写时参考过的公开资料（用于核对结论、避免凭印象下判断）：

| 主题 | 来源 |
| --- | --- |
| 构图：从规则到视觉冲击 | <https://visualwilderness.com/composition-creativity/nature-photography-composition-from-rules-to-visual-impact> |
| 向右曝光（ETTR） | <https://photographylife.com/exposing-to-the-right-explained> |
| 日系空气感风格的调色取向 | <https://blog.pinkoi.com/tw/lifestyle/tv0dirja/> |
| 锐化过度的识别与避免 | <https://www.digifotopro.nl/en/oversharpening-in-photography-how-to-avoid-it> |
| 人像修图的常见新手失误 | <https://petapixel.com/2017/07/29/common-amateur-portrait-retouching-mistakes/> |

> 说明：开发期曾把这些页面的正文抓取到 `.research/` 便于查阅，但**该目录不入库**
> （第三方正文有其版权，公开仓库不转载）。这里只保留来源链接与主题索引。

## 维护约定

* 新增参考条目请放进 `references/` 并在 `SKILL.md` 的索引里登记（自检 `tools/selftest_skills.py` 会校验一致性）；
* 条目要写成**可执行**的准则（"暗部抬 +10~15，超过 +20 会发灰"），不要写成美学散文；
* 修改技能包后需要重启服务才会生效（不做热加载，避免半改状态）。
