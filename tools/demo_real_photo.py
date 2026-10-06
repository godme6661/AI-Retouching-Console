"""真实照片链路演示：开图 → 离线抠图 → 调色 → 导出，并打印结果供验收留证。"""
from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8791"
PHOTO = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("DEMO_PHOTO", "")

c = httpx.Client(timeout=300)

r = c.post(f"{BASE}/api/open", json={"paths": [PHOTO], "mode": "new", "name": "示例·真机抠图"}).json()
print("开图    :", r["ok"], "源图尺寸", r["project"]["layers"][0]["src_size"])

r = c.post(f"{BASE}/api/ops", json={"ops": [
    {"op": "remove_bg", "engine": "birefnet", "feather": 1.5, "layer": "L1"}]}).json()
print("抠图    :", r["ok"], r["results"][0]["note"])

r = c.post(f"{BASE}/api/ops", json={"ops": [
    {"op": "auto_enhance", "strength": 45, "layer": "L1"},
    {"op": "temperature", "value": -8, "layer": "L1"},
    {"op": "shadows", "value": 12, "layer": "L1"}]}).json()
print("调色    :", r["ok"], [x["op"] for x in r["results"]])

p = c.get(f"{BASE}/api/state").json()["project"]
print("工程    :", p["name"], p["canvas"], "图层数", len(p["layers"]),
      "L1 步数", p["layers"][0]["op_count"], "有蒙版", p["layers"][0]["has_mask"])

ex = c.post(f"{BASE}/api/export", json={"format": "png"}).json()
print("导出    :", ex["path"])
prev = c.get(f"{BASE}/project/preview")
print("预览    :", prev.status_code, len(prev.content), "字节")
