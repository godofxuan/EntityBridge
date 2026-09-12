# 五分钟演示与复现

[播放字幕导览](EntityBridge-5min.mp4)。总长 300 秒，600 帧、2 fps，1600×1000；实际浏览器截图配合字幕，无音轨，不是一镜到底的实时录屏。截图哈希、章节和视频哈希保存在 [manifest](video_manifest.json)。

| 时间 | 内容 | 事实边界 |
|---|---|---|
| 0:00–0:45 | GLEIF 与 CH 的真实更名例子 | 编号支持评估真值，不预先声称 name-only 匹配成功 |
| 0:45–1:40 | 新封存测试与精确/模糊/Splink 对比 | 不掩盖 Splink 在干净现名称条件下落后 |
| 1:40–2:30 | 合成企业目录与逐对复核 | 页面名称、地址和分数为治理演示构造 |
| 2:30–3:40 | 撤销影响、候选版本、发布与追加事件 | 已实际通过浏览器点击验证；页面图片是操作结果 |
| 3:40–4:30 | 真实 10k 记录上的合成变更回放 | 展示实际重评量与全量等价，不是通用加速承诺 |
| 4:30–5:00 | 旧 ID 全部去向与固定历史版本 | 历史保留原 3 个成员，当前归属为纠正后的结果 |

## 自己操作

从项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m entitybridge.cli demo
```

访问 `http://127.0.0.1:8000`。已有演示数据库保留上次操作的历史，启动不会重置用户判断。首次启动有 5 条来源、3 个统一企业：Northstar 的服务公司经一个故意设置的高分桥误并进集团名称簇，Harbour 两条记录待复核。

1. 在复核页找到 NORTHSTAR ENERGY SERVICES LIMITED 对 NORTHSTAR ENERGY LTD，填写理由并“判定不同”。版本页出现候选，查询仍使用旧版本；点击“校验并发布”后企业数变为 4。
2. 在历史中对该判断“预览撤销影响”，查看每个预览企业的来源成员。当前示例 4→4：撤销不同企业判断的同时，对同依据自动边实施抑制，不会立即重新误合并。
3. 填写撤销理由，生成候选再发布。建立与撤销事件均保留。复核页显示该边已抑制；一条关系被撤销并不保证整个连通簇拆分，独立测试另外覆盖替代路径。
4. 当前任务已验证旧 ID `2159a810-de09-43fb-b60d-56ee422d4597` 返回两个当前去向；添加 `?revision=6f664cd7-b365-4259-84a1-c5f83f0980f0` 可看原始 3 个成员。其他新数据库会生成不同 UUID，使用其实际页面链接。
5. 版本页可导出固定 revision CSV。历史详情的记录证据链接也携带相同 revision；来源更新不会把历史证据悄悄替换为当前数据。

需要独立的新演示可指定新数据库路径，保留已有数据：

```powershell
$env:DATABASE_URL='sqlite:///artifacts/demo-fresh.sqlite'
.\.venv\Scripts\python.exe -m entitybridge.cli demo --port 8002
```

评分与增量回放命令见 README。重新编码现有截图需要自己提供带 libx264/libass 的 FFmpeg：

```powershell
.\.venv\Scripts\python.exe scripts/build_demo_video.py --ffmpeg .tools/media/ffmpeg.exe
```

原始 browser screencast 调用出现异常耗时，因此交付采用可逐帧复核的截图字幕导览。没有将静态截图合成的视频描述为持续实时操作录像。
