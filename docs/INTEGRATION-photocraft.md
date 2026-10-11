# 与 PhotoCraft 的关系、协作方式与外部引擎适配

> 本文记录与 [storytold/photocraft](https://github.com/storytold/photocraft) 的协作设计。
> **两个项目相互独立、各自许可**；这里既不是 fork，也不是它的插件，只是"可以互相接上"。

## 一、它是什么（事实，2026-10-11 实测）

| 项 | 值 |
| --- | --- |
| 定位 | Rust 写的 Photoshop 类桌面编辑器，自称 clean-room 重实现 |
| 许可 | `MIT OR Apache-2.0`（与本项目 MIT 兼容） |
| 规模 | 30 个 crate、1099 个 `.rs` 文件 |
| 热度 / 成熟度 | 44.6k star；但 **2026-09-30 才创建**、版本 0.7.0、1105 个未关闭 issue，**官方自称 early alpha** |
| 对外接口 | `photocraft-cli`：无头 `convert / info / run --cmd / batch / droplet / commands / mcp / serve` |
| 命令注册表 | **实测 858 条**（其中 140 条 enabled）：layer 206、filter 124、edit 77、image 64、type 59… |
| 它自认的最大缺口 | **AI 与生成式能力（其 `docs/gaps.md` 的 G15）**、15 个缺失工具、排版深度、插件兼容 |

## 二、为什么是互补而不是竞争

- 它缺的那一层（AI / 生成式）**正是本项目的全部**；
- 本项目缺的那些（PSD 图层、智能对象、排版、矢量、GPU 合成）**正是它的强项**；
- 它的 README 明确写着 "Built for agents"、"Let an agent drive it over MCP"——**它主动为被 AI 驱动而设计**。

所以正确的关系是：**本项目 = 大脑与审美层；PhotoCraft = 重型执行引擎之一。**

## 三、三种协作方式

### 方式 1：引擎委派（已实现，进程边界）
本项目把一条指令转发给外部引擎，产物作为**新图层**回到当前工程。

```bash
# 1) 装 PhotoCraft（下载 release 里的 portable 包，或从官网安装）
# 2) 让本项目找到它（二选一）
set PHOTOCRAFT_CLI=D:\path\to\photocraft-cli.exe          # 环境变量
# 或在 config/settings.json 里：
#   "external_engines": { "photocraft": { "enabled": true, "cli": "D:\\path\\to\\photocraft-cli.exe" } }

# 3) 查引擎状态与命令
curl http://127.0.0.1:8760/api/engine/status
curl "http://127.0.0.1:8760/api/engine/commands?filter=blur&limit=5"

# 4) 执行一条命令（产物自动作为新图层加入）
curl -X POST http://127.0.0.1:8760/api/engine/run -H "Content-Type: application/json" ^
     -d "{\"command\":\"filter.gaussianBlur\",\"params\":{\"radius\":2}}"
```

实测（本机）：`image.adjustments.colorLookup.list` 执行 **0.14 秒**，产物 77 KB，
作为 `L2` 图层进入工程，**撤销可一并回退**（走原有快照机制）。

**设计纪律**（见 [DECISIONS.md](DECISIONS.md) D9）：
- **默认关闭**：它处于早期阶段，不能成为本项目核心依赖；
- **进程边界**，不做 FFI：它崩溃不带走本服务，本仓库不引入 Rust 工具链；
- **不进管线**：引擎调用不写进图层算子列表，因此**可重放性不被破坏**；
- 产物走与手动导入完全相同的路径，所以撤销 / 导出仍然可靠。

### 方式 2：MCP 互相驱动
- 本项目已自带 MCP 服务器（见 README「MCP」一节）：`command_list / command_run / command_batch /
  doc_inspect / render_preview / render_viewport / export_image / undo / redo / engine_*`。
- PhotoCraft 也自带 MCP 服务器：`photocraft-cli mcp`。
- 于是**两者都能被同一个 agent 驱动**：本项目负责审美判断与轻量精修，PhotoCraft 负责重活。
  也可以让本项目通过 `engine_run` 间接驱动它，形成一个两层执行栈。

### 方式 3：手工交接（零代码）
- 本项目做 AI 优先的批量与审美，PhotoCraft 做需要手绘/钢笔/文字排版的重手工活。
- 交接靠导出图片；若要 PSD 往返，注意它的 **PSD 保真被自己列为头号缺口（blocks beta）**，
  不要承诺无损。

## 四、风险与边界（务必知道）

1. **早期阶段**：11 天 4.4 万星 = 高热度也是高变动；命令 id / 参数都会变。故默认关闭。
2. **不要再分发它的二进制**：本项目只调用你本机安装的它，不打包、不转售。
3. **它的插件是 WASM**，不能写 Python 插件；要扩展它得写 Rust/WASM。
4. **许可表述**：它遵循 `MIT OR Apache-2.0`；引用时按"独立工具"表述，不暗示关联或背书。
5. **单写者原则**：不要同时让两边的 UI 改同一个文件再互相覆盖——本项目按"配方 + 源图"管理，
  交出去的只是渲染结果。

## 五、还没做的（见 [GAPS.md](GAPS.md) G1）

让**对话里的 AI 自己决定**调用外部引擎，需要引入一类显式的"非重放算子"并想清楚撤销粒度。
在没想清楚之前不做——因为那会破坏本项目最核心的可复现性。
