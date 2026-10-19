# douyin-live-stream-trace

多角度采集抖音直播间数据，识别推流软件、分析流媒体协议、检测 CDN 特征。支持 OBS、FFmpeg、抖音直播伴侣、XSplit、vMix 等主流推流工具的指纹识别。

**仅供学习与技术研究。** 本项目以抖音直播为研究对象，帮助理解 FLV 容器格式、AMF0 协议、RTMP 推流、CDN 分发等流媒体技术的实际运作方式。请勿用于任何违反平台服务条款或法律法规的用途。

## 效果示例

```
══════════════════════════════════════════════════════
  直播间 #376389454958 分析报告
══════════════════════════════════════════════════════

  推流软件    抖音直播伴侣 (ByteDance Media SDK)
  置信度      高（encoder + app_id 交叉验证）
  分辨率      1088 × 1920
  编码器      H.264 / AAC
  CDN         字节跳动自建 CDN
  双流检测    未发现异常

══════════════════════════════════════════════════════
```

同时生成 HTML 暗色主题可视化报告（`reports/unified_report_*.html`），含完整证据链与参数详情。

## 快速开始

```bash
pip install requests

# 可选：浏览器自动化（推荐，自动处理签名鉴权）
pip install playwright && playwright install chromium
```

```bash
# 一键分析（推荐参数）
python analyze_room.py --room 376389454958 --playwright --format both

# 启用双流异常检测
python analyze_room.py --room 376389454958 --playwright --format both --detect-dual-stream
```

`--room` 接受直播间 URL 中的数字部分：

```
https://live.douyin.com/376389454958
                        ^^^^^^^^^^^^
```

## 工作原理

从四个维度采集数据，交叉验证后由统一报告引擎汇总结论：

| 维度 | 方式 | 获取信息 |
|------|------|----------|
| 平台 API | `reflow/info` + `web/enter` | app_id、client_version、推流类型标记 |
| 观众拉流 | 下载 FLV 样本，解析 onMetaData | encoder、分辨率、帧率、编码参数 |
| CDN 特征 | HTTP 响应头 + 流 URL 域名 | CDN 服务商、推流源特征 |
| RTMP 协议 | tshark 捕获 TCP/1935 流量 | flashVer、tcUrl（仅本机推流） |

## 可识别的推流软件

| 软件 | 识别方式 | 置信度 |
|------|----------|--------|
| 抖音直播伴侣 (ByteDance Media SDK) | app_id + encoder 组合 | 高 |
| OBS Studio | flashVer / encoder 指纹 | 高 |
| FFmpeg / libav | encoder 字段匹配 | 高 |
| XSplit / vMix / Wirecast / Streamlabs | flashVer 关键词 | 高 |
| PRISM Live Studio | encoder 特征 | 中 |
| 其他 | 原始 encoder 值回退匹配 | 低-中 |

## 项目结构

```
├── analyze_room.py          # 主入口（一键全角度分析）
├── flv_analyzer.py          # FLV 直播流在线分析
├── parse_flv_metadata.py    # FLV 离线元数据解析
├── dual_stream_detector.py  # 双流异常检测
├── rtmp_capture.py          # RTMP 推流分析
├── monitor_reflow.py        # API 状态监控
├── unified_report.py        # 统一报告生成
├── auth_playwright.py       # 浏览器自动化鉴权
├── config.py                # 共享配置
├── amf0.py                  # AMF0 协议解析器
└── mappings/                # 规则映射表（JSON，可扩展）
```

## 学习价值

本项目覆盖的技术点及对应代码：

- **FLV 容器格式** — Tag 结构、onMetaData 解码 → `parse_flv_metadata.py`
- **AMF0 协议** — 13 种数据类型编解码 → `amf0.py`
- **RTMP 协议** — 握手过程、connect 参数提取 → `rtmp_capture.py`
- **HTTP API 分析** — 多域名 fallback、请求签名、cookie 鉴权 → `network.py`、`auth_playwright.py`
- **CDN 识别** — 响应头特征匹配、域名模式分析 → `flv_analyzer.py`
- **指纹识别** — 多源交叉验证与置信度评级 → `flv_analyzer.py`
- **数据关联** — 多维度聚合、证据链构建、报告生成 → `unified_report.py`

建议阅读顺序：`amf0.py` → `parse_flv_metadata.py` → `flv_analyzer.py` → `analyze_room.py`

## 依赖

| 依赖 | 必需 | 说明 |
|------|------|------|
| Python 3.10+ | 是 | |
| `requests` | 是 | |
| `playwright` | 否 | 浏览器鉴权 |
| [tshark](https://www.wireshark.org/) | 否 | RTMP 分析 |
| [ffprobe](https://ffmpeg.org/) | 否 | 深度流分析 |

## 文档

- [脚本使用说明.md](脚本使用说明.md) — 完整使用手册
- [analysis_report.md](analysis_report.md) — 工具链分析与改进记录
- [reference_repos.md](reference_repos.md) — 相关开源项目汇总

## 已知限制

- 部分接口需要 `msToken` / `a_bogus` 签名（`--playwright` 自动处理）
- RTMP 分析仅适用于本机推流场景
- CDN 中转可能剥离部分元数据
- 规则基于已知特征匹配，新版软件需更新 `mappings/`

## 参考开源项目

| 项目 | 语言 | 说明 |
|------|------|------|
| [HaoDong108/DouyinBarrageGrab](https://github.com/HaoDong108/DouyinBarrageGrab) | C# | 代理方式获取弹幕，支持 WebSocket 和 HTTP 轮询，含完整 Protobuf 定义 |
| [scx567888/live-room-watcher](https://github.com/scx567888/live-room-watcher) | Java | 直连方案，使用 Playwright 拦截 wss URL，支持弹幕/礼物/点赞/关注等 |
| [LuckyPuppy514/douyin-live-danmu](https://github.com/LuckyPuppy514/douyin-live-danmu) | Python | Python 实现的弹幕抓取 |
| [wbxl2000/douyin-live](https://github.com/wbxl2000/douyin-live) | - | 抖音直播相关工具 |

## License

MIT
