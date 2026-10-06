# RAW 样本目录（可选）

部分自检与工具需要一张真实 RAW（CR3/NEF/ARW…）才能验证解码路径：

* `tools/selftest_raw.py` —— 真机 CR3 项（内嵌预览字节一致、全尺寸预览）
* `tools/e2e_check.py` —— RAW 实机检查、24MP 输出像素视口的渲染耗时对比
* `tools/bench_raw_decode.py`、`tools/check_cr3_orientation.py` —— 解码基准与方向

把任意一张 RAW 放进本目录即可（**本目录已被 .gitignore 排除，不会上传**），
或用环境变量指定别处：

```bash
# 指向一个目录（脚本会自己挑第一张 CR3）
export COGITATOR_RAW_DIR="/path/to/your/photos"
# 或直接指向一个文件
export COGITATOR_RAW_SAMPLE="/path/to/IMG_0001.CR3"
```

没有样本时，这些用例会**明确跳过**并打印原因，而不是失败。
