# 直播流分析工具集 · live-stream-trace family

一组围绕「直播流抓取 → 容器/协议与推流端分析 → 音画同步检测 → HTML 报告」的自研工具。
**主项目是 [`live-stream-trace/`](live-stream-trace/README.md)**（插件化多平台 + 网页系统），
其余目录是它的前身（抖音版 / 小红书版）与专项工具（RTMP 源流体检）。

> ⚠️ **仅供学习、技术研究与自有流排查**。使用前请先读下面的《合规与免责声明》。

---

## ⚠️ 合规与免责声明（请先读）

1. **用途限定**：本项目仅用于**学习、技术研究与自有直播流的排查**（例如排查自己推流的音画不同步、
   码率/帧率异常、时间戳跳变、CDN 回源问题）。
2. **不含第三方内容**：仓库**不附带**任何第三方直播视频、主播截图或真人画面；测试数据均为
   ffmpeg 合成的测试画面（`live-stream-trace/tests/media/test_d*.flv`），文档截图为脱敏后重制。
3. **不含任何凭据**：仓库不包含 Cookie、Token、登录态或带签名的拉流地址；`cookies/`、`*.cookie`、
   `.env`、`*.key` 已在 `.gitignore` 中排除。使用者需为**自己有权访问**的内容自行配置凭据。
4. **不提供绕过手段**：本项目不提供也不鼓励破解签名、绕过风控/鉴权、盗链、批量抓取或规避访问控制；
   解析只走平台公开接口与公开页面。
5. **你的合规义务**：在采集或分析任何内容前，请自行确认**已获得授权**，并遵守目标平台的用户协议、
   接口/robots 条款与所在地区法律法规（包括但不限于个人信息保护、著作权、肖像权）。
   **严禁用于商业性数据采集、监控、人肉/隐私侵犯等场景。**
6. **免责**：本项目按「现状」（AS IS）提供，不附带任何明示或默示担保；作者不对因使用本项目产生的
   任何直接或间接损失与法律责任负责。
7. **权利人下架**：若你是内容权利人或平台方，认为本仓库存在不当内容，请通过 Issue 联系，核实后会尽快移除。

---

---

## 1. 项目构成

| 目录 | 定位 | 入口 | 文档 |
|---|---|---|---|
| **`live-stream-trace/`** | **主项目**：插件化多平台（抖音 / 小红书 / 哔哩哔哩 / 淘宝 / 京东 / 通用 RTMP）；网页系统：首页报告、`/live` 实时音画延迟监测、`/sync` 上传样本检测 | `./run.sh <链接>`、`./start_web.sh 8899` | [README](live-stream-trace/README.md) |
| `douyin-live-stream-trace/` | 抖音独立版（前身）：RTMP 推流端抓包、礼物/榜单探针、Playwright 鉴权 | `python analyze_room.py` | [README](douyin-live-stream-trace/README.md) |
| `xiaohongshu-live-stream-trace/` | 小红书独立版（前身）：Cookie / 档位 / 源流 | `python analyze_room.py` | [README](xiaohongshu-live-stream-trace/README.md) |
| `rtmp-h264-stream-trace/` | 裸 RTMP/RTMPS H.264 源流体检（已以适配器形式并入主项目 `platforms/rtmp.py`） | `./run.sh <rtmp://…>` | [README](rtmp-h264-stream-trace/README.md) |
| `archive/patches_20261015/` | 一次性改造补丁脚本（**仅本地留档，不随仓库分发**） | — | — |
| `HANDOVER.md`、`SESSION_HANDOVER_*.md` | 开发交接记录（**仅本地留档，不随仓库分发**） | — | — |

---

## 2. 主项目能做什么

- **一条输入**（直播间链接 / 房间号 / 分享口令）→ 解析直播间 → 档位探活 → 采样 → FLV 元数据解析
  → 音画同步 → 生成 **HTML（暗色、内嵌播放器、历史可查）+ 文本**报告。
- **音画同步两层口径**（关键设计）：
  - *时间戳级*：流内 PTS（不含播放器 / 本地输出链延迟）；
  - *内容级*：人脸嘴部运动 × 音频包络互相关，**完全不看时间戳**，用于发现「推流前管线」已烘焙进的失同步。
- **🎯 健康片段自动选段**：持续捕获 → 滑窗打分（人脸覆盖/稳定性/嘴部运动/语音活动/相关性/特异性）
  → 导出小播放器片段 + 人脸/嘴部截图 → 在片段上复测偏移；报告中直接展示「健康片段内容 + 检测过程」。
- **测不出来就持续测**：内容级（健康片段）若得不出确定结论，自动续采重测，默认 **300s 超时**
  （`--measure-timeout`，`0` = 只测一次），并记录轮数/耗时进报告。
- **`/live` 实时监测窗口**：边播边测，0.5s 级刷新流内 A/V 偏移、码率、帧率、时间戳跳变。
- **`/sync` 上传样本检测**：本地视频（flv/mp4…）两层级音画同步检测。

---

## 3. 快速开始

```bash
# 主项目（首次会自动建 .venv 并装依赖）
cd live-stream-trace
./run.sh 'https://live.bilibili.com/5440'      # CLI：链接 / 房间号 / 分享文案
./run.sh 'rtmp://127.0.0.1:1935/live/streamkey' --no-lip-sync

# 网页系统（一框输入 → 报告 → 历史 / 实时窗口 / 上传检测）
./start_web.sh 8899                             # → http://localhost:8899/
#   /           首页（输入 → 生成报告 → 历史）
#   /live       实时音画（PTS）延迟监测
#   /sync       上传样本音画同步检测
```

### 环境要求

| 依赖 | 说明 |
|---|---|
| Python 3.14 | 各项目自带 `.venv` |
| `numpy` + `opencv-python-headless` **4.x** | 内容级检测需要；**5.x 移除了 Haar 级联**，必须 4.x。国内装：`pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com …` |
| `playwright` + `playwright install chromium` | 抖音 / 小红书需拦 `web/enter`、`reflow/info` 接口 |
| `ffmpeg` / `ffprobe` | 采样、抽帧、片段导出。**优先按 PATH 查找**，其次 `/opt/ffmpeg/bin`；把 ffmpeg 放进 PATH（或改代码里的 `FFMPEG_CANDIDATES`）即可 |

### 凭据与数据不入库

- Cookie：放 `*/cookies/<platform>.cookie`，或环境变量 `LST_COOKIE_<PLATFORM>`；`.gitignore` 已排除 `cookies/`、`*.cookie`、`.env`、`*.key`。
- 运行产物（捕获样本 `captures/`、报告 `reports/`、上传 `uploads/`、`.venv/`、`tests/.out/`）同样不入库。

---

## 4. 测试

```bash
cd live-stream-trace
./.venv/bin/python tests/integration_report.py        # 报告流水线集成（含内容级板块）
for f in tests/unit_*.py; do ./.venv/bin/python "$f"; done   # 9 个内容级单测
bash tests/verify_web_ui.sh                           # 网页系统端到端（含真实分析）
bash tests/verify_unified_rtmp_cli.sh                 # RTMP 全链路（本地合成本，无需外网）
./.venv/bin/python tests/verify_rtmp_e2e.py           # RTMP 适配器端到端
```

---

## 5. 打包、搬运与发布口径

**搬运（保留全部提交历史）**

```bash
git bundle create live-stream-trace.bundle --all
# 目标机器上：
git clone live-stream-trace.bundle live-stream-trace && cd live-stream-trace
git remote set-url origin <你的仓库地址> && git push -u origin master
```

**改为「可公开发布」时建议剔除的内容**

| 内容 | 原因 |
|---|---|
| `live-stream-trace/tests/media/xhs_room_long.flv`（23MB）、`xhs_room.flv` | 公开直播间抓取的**第三方内容** |
| `live-stream-trace/docs/frames/*`（真人截图 / 小片段） | 含可辨识的第三方人物画面 |
| `cookies/`、`captures/`、`reports/` | 个人登录态与本地数据（默认已不入库） |

### 发布时不导出的内容

`HANDOVER.md`、`SESSION_HANDOVER_*.md`、`archive/` 是开发过程留档，公开仓库不包含它们；
第三方直播样本与真人截图已在下表中列出并被移除。

---

## 6. 版权与许可

各子项目 README 均声明 **MIT**。根目录尚未放置统一的 `LICENSE` 文件——补之前需要先确定版权归属人。

**使用、克隆或分发本仓库即视为已阅读并接受上文《合规与免责声明》。**
