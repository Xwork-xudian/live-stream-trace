# 部署说明（本机已完成的部署记录）

部署目录: `douyin-live-stream-trace/`（源码来自 https://github.com/happyplum/douyin-live-stream-trace ）

## 1. 运行环境

| 项目 | 状态 |
|------|------|
| Python | 3.14.7（venv: `.venv/`） |
| requests | 已安装 2.34.2 |
| playwright | 已安装 1.63.0 + Chromium 153.0.8010.12（`~/.cache/ms-playwright`） |
| Chromium 系统依赖 | 已通过 `playwright install-deps chromium` 安装（libnspr4 / libnss3 / 字体 / xvfb 等） |
| ffprobe / ffmpeg | 可用（`/opt/ffmpeg/bin`，已在 PATH） |
| tshark | 未安装（仅 RTMP 本机抓包用到，可选） |

## 2. 常用命令

```bash
cd douyin-live-stream-trace

# 一键全角度分析（推荐，自动浏览器鉴权，输出 HTML+文本报告）
./run.sh 376389454958 --format both

# 含双流异常检测
./run.sh 376389454958 --format both --detect-dual-stream

# 等价的手动调用
./.venv/bin/python analyze_room.py --room 376389454958 --playwright --format both

# 仅浏览器鉴权自检（验证 Chromium 链路）
./.venv/bin/python auth_playwright.py 376389454958

# 离线分析本地 FLV
./.venv/bin/python parse_flv_metadata.py --file captures/xxx.flv

# 监控开播（内部 room_id，默认轮询 30s）
./.venv/bin/python monitor_reflow.py --room-id 7640561997888637734
```

输出位置：`captures/`（原始数据、JSON、FLV 样本）、`reports/`（统一报告 txt/html）。

## 3. 已完成的验证

- 14 个模块全部导入成功（config/amf0/network/flv_analyzer/analyze_room 等）。
- `analyze_room.py --help`、`parse_flv_metadata.py --help` 正常。
- 用 ffmpeg 生成合成 FLV 跑通离线元数据解析与推流软件指纹识别（正确识别为 FFmpeg）。
- Chromium 无头启动成功；`auth_playwright.py` 实际访问直播间成功拦截 `web/enter`、拿到 23 个 cookie 与内部 room_id。
- 示例房间当前「已结束」，因此直播中的 FLV 采集未被触发；开播后再跑即可。

## 4. 注意

- 该工具仅用于技术学习研究，请勿用于违反平台条款或法律的用途。
- 抓流/抓包依赖直播间正在开播；未开播时报告会提示数据不完整。
- `--playwright` 首次运行需加载页面，单次约 10–30 秒。

## 5. HTML 报告增强（2026-10-12 更新）

- **拉流地址不再是一坨原始 JSON**：身份卡显示真实拉流 URL（默认画质 FLV，带复制按钮）。
- 新增「🔗 拉流地址全景」区块：调度概览（流名/默认画质/主备线路/调度 CDN/app_id/推流会话/P2P/加密）、
  画质梯度表（含目标码率与转码实测码率）、全量拉流地址表（画质 × 协议 × 主备线路，一键复制）、
  URL 签名参数解析（expire 换算为可读时间）、播放器 SDK 参数解码、折叠的原始 JSON（stream_data 已解码）。
- **WSL 支持**：报告生成后自动打印 `\\wsl.localhost\...` 形式的 Windows 路径；
  `analyze_room` / `open_report.sh` 会调用 Windows 默认浏览器打开（原来 webbrowser 在 WSL 下静默失败）。
- 证据文件统一放在报告文件夹的 `evidence/` 子目录，「关联文件」列表可点击。

Windows 打开方式（任选）：

```bash
./open_report.sh                       # 打开最新报告
./open_report.sh reports/unified_report_xxx/index.html   # 打开指定报告
```

或把生成的 Windows 路径直接粘贴到 Windows 浏览器/资源管理器地址栏。

## 6. 内嵌播放器与源流地址（2026-10-12 更新）

**内嵌播放器**：报告内置 flv.js（HTTP-FLV）+ hls.js（HLS）双内核（随报告分发于 `assets/`，缺失时自动回退 CDN）。
点击任意「▶ 播放」按钮或拉流地址即可实时播放；状态栏显示解码参数（分辨率/编码器/下载速率）；
自动诊断过期(403)、下播(404)、跨域拦截、编码不支持(HEVC)等错误并给出建议。

**源流（未转码）地址**：拉流地址全景表自动构造并附播放按钮。
原理：ao（纯音频）档的路径是无画质后缀的 `stream-<id>.flv`（主播端原生推流），
服务端用 query 参数 `only_audio=1` 剥视频；防盗链签名按路径校验，去掉 `only_audio` 与
`eupf_relay_mode` 参数即得到完整源流，不经云端转码二压。

实测注意：若主播以 H.265/HEVC 推流（ffprobe 源流显示 hevc，而蓝光 or4 显示 h264 即为云端转码），
网页端 flv.js 无法解码 HEVC，播放器会给出提示；此时请复制源流地址用 VLC / ffplay 播放，
或使用蓝光/超清等 H.264 转码档。H.264 推流的房间源流可直接在网页播放。

**本地服务兜底**：`./serve_report.sh [端口] [报告路径]` 启动 `http://localhost:8899` 并用 Windows 默认
浏览器打开（抖音拉流 CDN 已放行 `Access-Control-Allow-Origin: *`，file:// 打开一般也能直接播放）。

## 7. ffplay 播放源流

```bash
./play_source.sh              # 最新会话的源流（主线），自动探测编码并启动 ffplay
./play_source.sh --backup     # 备线路源流
./play_source.sh <直播间号>    # 指定会话

# 手动播放报告里复制的地址
ffplay -fflags nobuffer -flags low_delay -framedrop "http://...flv"

# 无损录制源流（不改码流，仅封装）
ffmpeg -i "http://...flv" -c copy -bsf:a aac_adtstoasc source.flv
```

Windows 原生：`winget install Gyan.FFmpeg` 后在 PowerShell 中 `ffplay.exe "URL"`（URL 含 & 必须加引号）。
注意：拉流地址跟随推流会话，**房间下播即 404**（并非 7 天有效期都能用）；403 = 签名/时间过期。

## 8. 音画同步检测（av_sync_detector.py）

双层测量，毫秒精度，作为直播流分析的子工具。

### 用法

```bash
# ① 分析已有 FLV 样本（标签级）
./.venv/bin/python av_sync_detector.py --file captures/flv_sample_xxx_FULL_HD1.flv

# ② 在线拉流分析（推荐：20~30s 采样才能评估漂移）
./.venv/bin/python av_sync_detector.py --url "http://...flv" --duration 22

# ③ 附加解码级验证（ffplay 无头实播，逐帧 A-V，地面真值）
./.venv/bin/python av_sync_detector.py --url "http://...flv" --duration 22 --decode-check --decode-seconds 12

# ④ 与房间分析一起跑（报告自动出「⏱ 音画同步检测」区块）
./run.sh <直播间号> --av-sync --format both
```

输出：`captures/av_sync_analysis_<ts>.json`、`captures/av_sync_report_<ts>.txt`；
统一报告 HTML/文本中自动渲染，含感知判定徽章与门限说明。

### 指标含义（关键）

| 指标 | 含义 |
|------|------|
| **流内偏移（主指标）** | 音频 PTS − 视频 PTS 的中位数（ms）。内容固有缺陷，= 感知同步的定义量，EBU R37 / ITU-R BT.1359 的度量对象 |
| **播放器残差** | ffplay 逐帧 A-V：现代播放器用音频时钟对齐视频、吸收**恒定**偏移后的剩余误差。残差小 ≠ 流没问题 |
| **音频时间线漂移** | 音频时间戳相对真实音频时长（AAC 帧数精确已知）的速率 ms/min。恒定偏移可被播放器修正，**漂移不可**，最危险 |
| 峰值偏移 / 交错趋势 | 偏移波动范围与趋势（短样本易受帧量化影响，仅参考） |
| 帧时间戳回退/重复帧 | 抖音边沿拉流常见的回退重发（起播/续传重发上一段 GOP），解码端丢重复帧，通常不影响观看 |
| 到达节奏 | 媒体时间/墙钟 ≈1 为实时；>1.5 为非实时下发（文件/缓存），节奏指标不适用 |

### 感知门限（ITU-R BT.1359-1 / EBU R37，不对称窗口）

- EBU R37 广播建议：音频超前 ≤ +40ms、滞后 ≤ −60ms
- ITU-R BT.1359-1：可察觉 +45ms（超前）/ −125ms（滞后）；可接受 +90ms / −185ms
- 听觉超前比滞后更易被察觉，故窗口不对称

### 局限（必须知道）

本工具测的是**流内 PTS 级偏移**（感知同步的定义量，±1ms 分辨率），**不含本地播放链延迟**：
蓝牙耳机(+100~200ms)、电视图像处理(+10~40ms)、功放/声霸等会叠加到最终听感。
端到端「眼-耳」偏移 = 流内偏移（或播放器残差）+ 显示链延迟 − 音频输出链延迟。
若需端到端物理标定，须用「闪光+咔哒声」测试视频拍摄后分析（需本机扬声器/显示链路参与）。
