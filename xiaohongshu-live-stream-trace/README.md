# xiaohongshu-live-stream-trace

小红书（RED）直播流分析工具集 —— 仿 [douyin-live-stream-trace](../douyin-live-stream-trace) 结构实现。

采集小红书直播间信息、解析推流端指纹、获取**未转码源流**地址、检测音画同步，并生成带内嵌播放器的
暗色主题 HTML 报告。

**仅供学习与技术研究**，请勿用于违反平台条款或法律法规的用途。

## 与抖音版的差异

| 维度 | 抖音 | 小红书 |
|------|------|--------|
| 直播间信息 | reflow/info + web/enter API（需 msToken/a_bogus 签名） | **页面 SSR** `window.__INITIAL_STATE__`（移动端 UA 即可，无需签名） |
| 拉流地址 | 多画质档 × 主/备线路 × 多协议，源流需从 ao 地址去参构造 | 由 room_id 直接拼装：`live-source-play.xhscdn.com/live/{room_id}.flv` **本身即源流**（未转码） |
| 转码二压 | 云端生成 or4/Stage0T000hd/ld/sd 等档位 | 源流直接分发，页面另有 CDN 播放域名 |
| 鉴权 | 浏览器自动化处理签名 | 通常不需要；风控时可用 `--playwright` 兜底 |

## 快速开始

```bash
# 依赖（首次）
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m playwright install chromium   # 可选：--playwright 兜底需要

# 一键分析（房间号 / livestream URL / 主播主页 URL / xhslink 分享短链 都支持）
./run.sh <直播间号> --av-sync --format both

# 蹲守开播（未开播时每 60 秒重试，共 10 次）
./.venv/bin/python analyze_room.py --room <直播间号> --poll-retries 10 --poll-interval 60

# 只看拉流地址（不下载样本）
./.venv/bin/python analyze_room.py --room <直播间号> --max-bytes 0
```

## 命令行工具

```bash
# 房间解析（独立调用，便于排查）
./.venv/bin/python xhs_room.py <直播间号|URL|短链>

# 分析本地 FLV 样本
./.venv/bin/python parse_flv_metadata.py --file captures/flv_sample_xxx_SOURCE.flv

# 音画同步检测（标签级 + 解码级，毫秒精度）
./.venv/bin/python av_sync_detector.py --file captures/flv_sample_xxx_SOURCE.flv --decode-check
./.venv/bin/python av_sync_detector.py --url "http://live-source-play.xhscdn.com/live/xxx.flv" --duration 22 --decode-check

# 用 ffplay 播源流
./play_source.sh            # 源流 FLV
./play_source.sh --hls      # HLS

# 打开报告 / 用本地 HTTP 服务打开（浏览器播放更稳）
./open_report.sh
./serve_report.sh
```

## 报告内容

- **身份卡**：主播昵称、房间 ID（来自 SSR）
- **🔗 拉流地址（小红书源流）**：源流 FLV / HLS，含探活结果（HTTP 状态、Content-Type、FLV 魔术字节），
  支持**点击播放**（flv.js / hls.js 双内核）与一键复制
- **推流端识别**：基于 FLV onMetaData（encoder/flashVer 等）交叉验证
- **⏱ 音画同步检测**：流内偏移（主指标）+ 播放器残差 + 感知门限判定（EBU R37 / ITU-R BT.1359）
- 证据链、关联文件（原始 JSON/TXT 可点击）

## 已知限制

- 小红书 Web 端未登录会弹登录墙；本工具走**移动端 UA** 的 SSR 数据（社区通用做法），
  若遇风控可加 `--playwright`（无头 Chromium）或 `--cookies`
- 拉流地址随直播会话失效：**下播即 404**（与抖音一致）
- 源流编码可能是 H.265/HEVC —— 网页 flv.js 不支持，报告会给出提示（用 VLC/ffplay 播放）
- 直播列表/发现不在本工具范围内：请提供具体的直播间号或分享链接

## 目录结构

```
├── analyze_room.py            # ⭐ 一键编排器（Step1-6 + 音画同步）
├── xhs_room.py                # 直播间解析（SSR / 短链 / 主页）
├── xhs_auth_playwright.py     # 风控兜底（无头 Chromium）
├── av_sync_detector.py        # 音画同步检测（平台无关）
├── flv_analyzer.py            # FLV 在线分析 + 推流端指纹
├── parse_flv_metadata.py      # FLV 离线元数据解析
├── unified_report.py          # 统一报告（含内嵌播放器 / 拉流面板）
├── amf0.py / network.py / mappings_loader.py / capture_manifest.py
├── mappings/                  # 推流端 / CDN / 字段 映射表
├── report_assets/             # flv.js + hls.js（随报告分发）
└── run.sh / play_source.sh / open_report.sh / serve_report.sh
```

## License

MIT
