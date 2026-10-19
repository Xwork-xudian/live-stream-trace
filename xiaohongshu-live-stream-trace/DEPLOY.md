# 部署说明（xiaohongshu-live-stream-trace）

## 1. 环境

| 项目 | 状态 |
|------|------|
| 源码 | 仿 `douyin-live-stream-trace` 实现，平台无关模块直接复用 |
| Python | venv（`.venv/`），依赖 `requests` + `playwright`（可选兜底） |
| 播放器依赖 | `report_assets/flv.min.js`（HTTP-FLV）+ `hls.min.js`（HLS），随报告分发 |
| ffmpeg/ffplay | 复用环境内 ffmpeg 9.0.1（音画同步解码级验证、播放源流） |

## 2. 常用命令

```bash
cd xiaohongshu-live-stream-trace

# 一键分析（四种输入都可：房间号 / livestream URL / 主播主页 / xhslink 短链）
./run.sh <直播间号> --av-sync --format both

# 蹲守开播
./.venv/bin/python analyze_room.py --room <直播间号> --poll-retries 10 --poll-interval 60

# 只解析房间 + 拉流地址（排查用）
./.venv/bin/python xhs_room.py <直播间号|URL|短链>

# 播源流 / 打开报告
./play_source.sh [--hls]      # ffplay 播源流（HEVC 也能解）
./open_report.sh              # Windows 浏览器打开最新报告
./serve_report.sh             # 本地 HTTP 服务打开（播放更稳）
```

## 3. 技术要点（实测确认）

1. **直播间信息**：移动端 UA 请求 `https://www.xiaohongshu.com/livestream/{room_id}` 或
   `/user/profile/{user_id}`，页面 SSR 内嵌 `window.__INITIAL_STATE__`，
   其中 `liveStream.roomData.roomInfo` 含标题与 `deeplink`；`deeplink` 的 `flvUrl` 参数给出 room_id。
   **无需签名**，实测无登录墙（伪造房间号返回 `liveStatus: success` + 空 roomData，已正确处理）。
2. **源流地址**：`http://live-source-play.xhscdn.com/live/{room_id}.flv` 即**未转码源流**；
   同 ID 换 `.m3u8` 得 HLS。备选域名 `live-play.xhscdn.com`（302 跳转）已内置回退。
3. **CORS**：实测 `live-source-play.xhscdn.com` 回显 Origin（含 `null`），因此报告内嵌播放器
   在 `file://` 与 `localhost` 两种打开方式下都能直接播放。
4. **复用抖音版能力**：FLV 元数据解析、推流端指纹、音画同步检测（标签级 + 解码级）、
   HTML 报告（身份卡 / 证据链 / 内嵌播放器）全部复用，仅替换平台相关部分。

## 4. 已完成的验证

离线端到端（本地模拟 SSR 页面 + 本地 FLV 源流，走完全部 Step1-6）：

| 环节 | 结果 |
|------|------|
| 输入规范化（房间号/短链/主页/livestream URL/deeplink） | ✅ |
| SSR 解析（合成结构 + 真实站点响应） | ✅ 真实站点返回 SSR，无登录墙 |
| 拉流地址构造 + 探活（HTTP/Content-Type/FLV 魔术字节） | ✅ |
| FLV 样本下载 + 元数据解析 | ✅ 识别推流端 `FFmpeg / libRTMP` |
| 音画同步检测 | ✅ 流内 -7.0ms / 播放器残差 -5.06ms |
| 统一报告（小红书品牌 + 源流面板 + 播放按钮） | ✅ 全项核验通过 |
| 内嵌播放器实测（无头 Chromium 点击播放） | ✅ `✅ 解码中: 640x360 \\| avc1.42c01e / mp4a.40.2` |
| 真实 CDN CORS | ✅ 回显 Origin（含 null） |

**待验证**：真实在播直播间的端到端跑通（需要一个当前在播的房间号/分享链接）。

## 5. 注意

- 拉流地址随推流会话失效：**下播即 404**，签名有效期与直播状态无关。
- 源流可能是 H.265/HEVC（抖音版实测常见），网页 flv.js 不支持，报告会提示改用转码档或 VLC/ffplay。
- 小红书 Web 端未登录会弹登录墙（笔记/发现页），但**直播间页面本身可用移动端 UA 直接拿 SSR**。
- 本工具仅用于技术学习研究。

## 6. 真实直播间验证结果（2026-10-12）

测试房间：`影视飓风MediaStore`（`https://www.xiaohongshu.com/livestream/dynpath0PAwhexh/570493467444214694`）

| 环节 | 结果 |
|------|------|
| 直播间解析（两段式 URL `/livestream/{token}/{room_id}`） | ✅ 房间 570493467444214694，标题「去了一趟，影视飓风直播间」 |
| SSR 状态 | ✅ 移动端 UA 直出，无登录墙 |
| 拉流地址探测 | ✅ 源流 FLV / HLS / 主备共 5 条全部 HTTP 200（FLV 魔术字节校验通过） |
| pullConfig 官方矩阵 | ✅ h264 原画 HD × 4 条：HLS 主线、FLV 主线、FLV 备线（腾讯云 `-bak-tx`）、FLV 备线（华为云 `-hw`） |
| 样本下载 | ✅ 4~6MB，Server: `MCP_VCLOUD_LIVE`（字节 MCP 直播云） |
| 推流端识别 | ✅ **OBS Studio**（onMetaData encoder） |
| 音画同步 | ✅ 流内 -16.0 ms / 播放器残差 -3.56 ms → 同步良好 |
| 报告内嵌播放器 | ✅ 无头 Chromium 点击播放实测：`解码中 1080x1920 │ avc1.64002a / mp4a.40.2` |

报告：`reports/unified_report_20261012_202954/index.html`

## 7. 与抖音版的能力对照

复用的通用能力（直接拷贝即可用）：FLV 元数据解析、推流端指纹、音画同步检测、
HTML 报告框架（身份卡 / 证据链 / 内嵌播放器 / WSL 路径提示）。

小红书特有的三处（本项目实现）：
1. **SSR 路线**取直播间信息（替代抖音 reflow/web-enter API + 签名）；
2. **pullConfig 矩阵**解析（编码 × 线路 × 协议 × 画质，含腾讯云/华为云备线）；
3. **源流直取**：`live-source-play.xhscdn.com/live/{room_id}.flv` 无需去参构造。

## 8. 报告修复与增强（2026-10-12 晚）

### 8.1 档位（转码流）地址 —— 实测规律

档位由「房间号 + 后缀」构成，每档都有 FLV 与 HLS 两种：

| 档位 | FLV | HLS | 实测参数 |
|------|-----|-----|----------|
| 原画（H.264） | `{room}.flv` | `{room}.m3u8` | h264 1080x1920 ≈4-10 Mbps |
| 原画（HEVC） | `{room}_orig.flv` | `{room}_orig.m3u8` | hevc 1080x1920 |
| 标清/流畅 | `{room}_sd.flv` | `{room}_sd.m3u8` | h264 200x360 ≈300 kbps |

其他后缀（`_ld/_hd/_fhd/_uhd/_1..4`）CDN 直接挂起 → 不存在。
分析时自动探测（`--no-tier-probe` 可跳过），报告出「档位（实测）」表：编码 / 分辨率 / 码率 / 播放按钮。
HEVC 档网页 flv.js 不支持，播放时给出明确提示（用 VLC/ffplay 或选 H.264 档）。

### 8.2 账号与房间信息

| 字段 | 来源 | 无 Cookie 时 |
|------|------|--------------|
| 昵称 / UID(host_id) / 头像 | 页面 SSR deeplink | ✅ |
| 房间ID | 页面 SSR | ✅ |
| 实时人数 / 在线成员 / 点赞 | SSR `displayViewerCount/MemberCount/PraiseCount` | ✅ |
| **开播渠道** | FLV onMetaData `encoder`+`platform` 推断 | ✅ |
| **版本号** | 同上（kasa_win_3.9.4→3.9.4(Kasa Win)、OBS→OBS 32.1.0） | ✅ |
| **直播时长 / 开播时间** | FLV tag 时间戳（流内绝对位置） | ✅ |
| **小红书号 / 粉丝 / 关注** | `live-room.xiaohongshu.com` user_card（需签名+登录） | ❌ 显示「需登录 Cookie」 |

开播渠道推断实例（三个真实直播间）：

| 房间 | encoder | platform | 结论 |
|------|---------|----------|------|
| 盗月社食遇记 | `kasa_win_3.9.4` | Windows | 小红书 PC 开播工具（Kasa） |
| 水月雨 | `820cfcfa`（iOS SDK） | iOS | **手机开播（小红书 App）** |
| 影视飓风 | `obs-output module (libobs 32.1.0)` | — | 第三方 OBS Studio |

三者推流服务器均为 `TLSS/2.0.0(qcloud)` = 腾讯云直播 SDK。

### 8.3 账号信息（粉丝/关注/小红书号）需要登录 Cookie

已接入签名（`xhs` 包的 `sign`），实测：无 Cookie 时接口返回 `-101 无登录信息`。
提供登录 Cookie 即可获取：

```bash
./run.sh "<直播间链接>" --cookies "a1=...; web_session=...; webId=..."
```

Cookie 获取：浏览器登录小红书 → F12 → Network → 任意 xiaohongshu.com 请求 → 复制 Request Headers 的 Cookie 整行。

### 8.4 用法简化（问题 4）

两个工具均支持**直接传链接**（位置参数或 `--room`）：

```bash
# 小红书：短链 / 长链 / 两段式 livestream URL / 主页 / 房间号
./run.sh "https://www.xiaohongshu.com/livestream/dynpath02FFqYPh/570492987146085538"
./run.sh "http://xhslink.com/xxxx"

# 抖音：分享链接 / 房间号
./run.sh "https://live.douyin.com/376389454958"
./run.sh "376389454958"
```

## 9. 本轮修复与增强（2026-10-12 深夜）

### 9.1 修复：软件判断被 CDN 域名污染 / 置信度偏低

| 问题 | 根因 | 修复 |
|------|------|------|
| 软件判断显示“小红书 / 小红书直播组件” | 指纹匹配把 **CDN 域名 `live-source-play.xhscdn.com` 里的 xhs** 当成软件特征 | ① 软件规则改为 `xiaohongshu\|xhs[-_ ]?(live\|push\|client)\|redlive`（去掉裸 `xhs`）② `flv_analyzer` **不再用 URL/域名匹配软件**（域名只用于 CDN 归属） |
| 置信度总是“低” | 规则不认识真实 encoder：`kasa_win_3.9.4`、`Lavf59.14.100`（FFmpeg 的 libavformat）→ 无高/中证据 → 走低置信兜底 | ① 补 Kasa / Lavf / libavformat 规则 ② **encoder 字段命中升为“高”级证据**（推流端自声明）③ `platform`/`server` 作为“中”级环境证据 |
| 客户端哈希 encoder 显示为软件名 | 原始值直接当软件 | 形如 `820cfcfa` 的哈希 → 显示“未知（客户端指纹 …）” |

修复后：MOFT 房 = **软件判断 FFmpeg / libRTMP，置信度 高，第三方推流 是**。

### 9.2 新增：RTMP 源流（多主机探测）

安卓抓包格式 `rtmp://live-play.xhscdn.com/live/{room}` 实测**时好时坏**（No such stream）；
`rtmp://live-source-play.xhscdn.com/live/{room}` 同样可拉且更稳。已实现**多主机依次探测**，
报告「拉流地址」面板逐条列出（可用/失败 + 实测编码分辨率 + 复制按钮）。

> 网页播放器不支持 RTMP，请用 VLC / ffplay，或 `./play_source.sh --rtmp`。

### 9.3 新增档位：安卓原画（HEVC·hcv5400）

`{room}_hcv5400.flv` = 安卓客户端原画档（实测 hevc 1080x1920）；现已加入档位探测列表
（原画 H.264 / 原画 HEVC / 安卓原画 hcv5400 / 标清 sd），逐档给出实测编码·分辨率·码率 + FLV/HLS 播放按钮。

### 9.4 Cookie 持久化 + 音画同步默认开启

- 登录 Cookie 存于 `.xhs_cookie`（已加入 .gitignore，权限 600），`config.DEFAULT_COOKIE` 自动加载，
  `--cookies` 可覆盖；启动时打印脱敏提示。有 Cookie 时报告直接显示 **小红书号 / 粉丝 / 关注 / 获赞与收藏 / 简介 / IP属地 / 累计观看(pv)**。
- 音画同步检测**默认开启**（`--no-av-sync` 可关闭）。

### 9.5 报告显示修复

- 长 UID / 哈希值不再溢出指标框（word-break + 自适应字号）
- 指标补充：粉丝 / 关注 / 获赞与收藏 / 累计观看 / 在线成员 / 点赞
