# 抖音直播间流分析工具 — 综合分析报告

> 生成时间: 2026-05-18（已更新，反映 Playwright 鉴权和双流检测功能）
> 分析对象: analyze_room.py / auth_playwright.py / dual_stream_detector.py / flv_analyzer.py / monitor_reflow.py / rtmp_capture.py / parse_flv_metadata.py / unified_report.py

## 工具链概览

| 脚本 | 视角 | 核心能力 |
|---|---|---|
| `analyze_room.py` | 一键编排 | 统一入口：7 步自动流程，Web 房间号 → 全角度采集 + 报告 |
| `auth_playwright.py` | 浏览器鉴权 | 无头 Chromium 拦截 web/enter，提取 cookies + msToken，处理 a_bogus |
| `dual_stream_detector.py` | 双流检测 | 多清晰度/多模式/跨协议对比，检测阴阳流和双流异常 |
| `flv_analyzer.py` | 观众拉流面 | 下载 FLV 流，解析 onMetaData + HTTP 响应头 + URL 域名特征 |
| `parse_flv_metadata.py` | 离线分析面 | 解析已有 FLV 文件元数据，生成编码器指纹 |
| `monitor_reflow.py` | 平台 API 面 | 轮询 reflow/info，开播瞬间抓取完整 JSON + 全量字段树 |
| `rtmp_capture.py` | 推流协议面 | tshark 抓 TCP/1935，提取 flashVer + 27 种软件指纹 |
| `unified_report.py` | 综合报告面 | 跨脚本关联采集结果，生成 HTML/文本统一报告（含字段中文解读） |

## 共享模块

| 模块 | 说明 |
|---|---|
| `amf0.py` | 统一 AMF0 解析器（13 种类型），ci_get 大小写不敏感查找，8192 迭代安全防护 |
| `config.py` | 集中配置：房间号、Headers、超时、10 条软件模式、9 条 CDN 模式、27 个 RTMP 关键词 |
| `network.py` | HTTP 工具：指数退避重试、流式下载、HEAD 请求 |

## API 端点

1. `live.douyin.com/webcast/room/reflow/info/` / `webcast-hl.amemv.com/webcast/room/reflow/info/` — params: live_id=1, room_id, app_id=1128
2. `live.douyin.com/webcast/room/web/enter/` / `webcast-hl.amemv.com/webcast/room/web/enter/` — params: app_id=1128, web_rid

## 已完成的改进

### P0 Bug 修复
- [x] parse_flv_metadata.py: push_software UnboundLocalError
- [x] flv_analyzer.py: fallback hints.get() 大小写+前缀问题
- [x] AMF0 解析器: 添加 8192 次迭代安全防护
- [x] unified_report.py: encoder_hints/width/height 大小写不敏感查找
- [x] rtmp_capture.py: 27 个二进制搜索关键词（含大小写变体）

### 重构
- [x] 提取共享模块 amf0.py（合并 3 份 AMF0 解析器，补全 13 种类型）
- [x] 提取共享模块 config.py（统一房间号、Headers、超时、模式）
- [x] 提取共享模块 network.py（重试退避、流式下载）
- [x] 4 个采集脚本：argparse + logging + 结构化 JSON 输出
- [x] unified_report.py：JSON 优先加载 + 文本回退 + argparse
- [x] Metis 审查：修复 JSON 路径双重嵌套、类型子串匹配错误

### 一键编排脚本
- [x] analyze_room.py：7 步自动流程（Playwright 鉴权 → 解析房间号 → reflow/info → web/enter → FLV 下载 → 元数据解析 → 双流检测 → 生成报告）
- [x] 部分失败容错：各步骤独立执行，单步失败不影响后续
- [x] Step 3 复用 Step 0/1 缓存避免重复 API 调用
- [x] 项目级 skill：`.opencode/skills/analyze-douyin-room/SKILL.md`

### 多域名 API 支持
- [x] config.py: 新增 `REFLOW_URLS` / `ENTER_URLS` 多域名列表（保留单 URL 向后兼容）
- [x] monitor_reflow.py: 自动尝试 live.douyin.com → webcast-hl.amemv.com
- [x] flv_analyzer.py: 自动尝试 live.douyin.com → webcast-hl.amemv.com
- [x] webcast-hl.amemv.com 的 reflow/info 无需鉴权即可返回数据（HTTP 200）
- [x] live.douyin.com 的 reflow/info 已返回 404，web/enter 需要 a_bogus（10011）

### Playwright 浏览器鉴权
- [x] auth_playwright.py: 无头 Chromium 拦截 web/enter 响应
- [x] 自动提取浏览器 cookies（含 httpOnly 的 ttwid）
- [x] 自动提取 msToken（从网络请求 URL）
- [x] analyze_room.py `--playwright` 参数集成（Step 0）
- [x] 成功处理 a_bogus 签名
- [x] Playwright 原始证据标准落盘（`playwright_enter_*` / `playwright_cookies_*`）

### 采集闭环化
- [x] analyze_room.py: 标准输出 `room_resolution_*`、`stream_url_raw_*`、`session_manifest_*`
- [x] dual_stream_detector.py: 标准保留多画质原始 FLV 样本与 HLS/DASH manifest 原文
- [x] unified_report.py: 跳过 `session_manifest`，避免把运行索引误并入业务结果

### 规则治理化
- [x] 新增 `mappings/` 目录，外置字段说明、推流软件规则、CDN 规则
- [x] 新增 `mappings_loader.py`，在加载时做最小结构校验
- [x] `monitor_reflow.py` / `unified_report.py` 共用同一份 `FIELD_DESCRIPTIONS`
- [x] `unified_report.py` 删除私有 CDN 规则列表，改为复用 `config.CDN_PATTERNS`

### Capture-First 多探针采集层
- [x] 新增 `gift/list`、`room/info`、`ranklist/audience` 三类原始探针
- [x] probe 产物可由 `analyze_room.py --capture-probes` 作为可选步骤接入 `session_manifest`
- [x] `gift/list` 已验证在线成功并可落盘
- [x] `room/info` / `ranklist/audience` 当前已确认存在房间状态 / 主播上下文 / 更完整鉴权边界，失败时以结构化错误返回，不拖垮主流程
- [ ] 当前仍未接入 `unified_report.py`
- [ ] 当前仍未做字段提炼与 `reflow/info` 差异分析

### 双流异常检测
- [x] dual_stream_detector.py: 多清晰度样本对比
- [x] 双模式对比：`need_double_stream=true` vs `false`
- [x] 跨协议对比：FLV vs HLS/DASH
- [x] SDK 画质级别提取（origin/uhd/hd/md/sd/ld）
- [x] 风险评分 0-100

### 报告优化
- [x] reflow 关键字段精确匹配（排除子串匹配噪音）
- [x] 字段中文解读列（FIELD_DESCRIPTIONS）
- [x] 路径作用域过滤（仅 data.room 直接子字段）
- [x] 证据链过滤 URL/图片等无意义字段

## 能力评估

| 能力维度 | 评级 | 说明 |
|----------|------|------|
| 元数据提取 | 强 | 22 个 onMetaData 字段全量导出，大小写不敏感 |
| 数据源识别 | 中强 | FLV URL 域名分析、HTTP 响应头、第三方推流检测 |
| 推流设备识别 | 中强 | 10 种已知软件模式 + 编码器原始值回退 |
| 跨源关联 | 中强 | unified_report.py 按 room_id+时间戳关联，JSON 优先，字段含中文解读 |
| API 鉴权 | 强 | Playwright 自动鉴权，处理 a_bogus；reflow/info 无需鉴权 |
| 双流检测 | 中 | 多维度对比（清晰度/模式/协议），仅限 Web 端可见画质 |

## 待改进（P1-P2）

### P1
- WebSocket 弹幕 wss+protobuf 抓取（需要 ACK 应答 + 心跳维持）
- `_X-MS-TK-STUB` 签名算法
- 三层房间 ID 解析（share link → room_id → web_rid）

### P2
- HLS/LLS 流分析
- Protobuf 消息解析（200+ 消息类型）
- WebcastControlMessage 直播结束检测
- 画质降级重试策略
- 代理支持

## 参考开源项目

详见 `reference_repos.md`（60+ 条目，8 个类别）。

核心参考：
- jwwsjlm/douyinLive (Go, WebSocket+Protobuf)
- saermart/DouyinLiveWebFetcher (Python)
- ihmily/DouyinLiveRecorder (Python, 多平台录制, a_bogus 签名)
- freeloop4032/douyin-live (Protobuf schema)
- chrippa/python-flashmedia (AMF0 参考)
