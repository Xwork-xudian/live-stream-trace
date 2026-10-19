# 验证脚本（live-stream-trace）

这些脚本是可复现的验证/回归套件，覆盖音画同步两层口径与通用 RTMP 源流。

```bash
cd live-stream-trace
./.venv/bin/python tests/unit_lip_sync.py          # 单元：互相关滞后恢复 + ROI 几何 + 包络
./.venv/bin/python tests/unit_flv_duration_jump.py # 单元：FLV 时间戳跳变下的时长估算
./.venv/bin/python tests/unit_face_backend.py       # 单元：关键点嘴部 ROI / 正脸度 / 纹理
./.venv/bin/python tests/unit_voice_activity.py    # 单元：语音/静音/BGM 区分
./.venv/bin/python tests/unit_roi_tracking.py       # 单元：ROI 逐帧跟踪几何与取样映射
./.venv/bin/python tests/verify_speaker_rescue.py   # 说话时段兜底改判（全段被 BGM 稀释时）
./.venv/bin/python tests/unit_occlusion.py          # 单元：嘴部遮挡（手/物体）判定
./.venv/bin/python tests/verify_adaptive_capture.py # 采样端规避：等到可测窗口再采样
./.venv/bin/python tests/unit_specificity_controls.py # 单元：特异性对照区（人脸框换算）
./.venv/bin/python tests/unit_aperture.py          # 单元：唇几何信号 + 稳健参照 + 跳过门禁
./.venv/bin/python tests/unit_openness.py          # 单元：唇线暗带信号（抗美颜）
./.venv/bin/python tests/verify_lip_vs_ts.py       # 受控实验：时间戳级漏报 vs 内容级检出
./.venv/bin/python tests/integration_report.py     # 集成：嘴型板块进入 HTML/文本 + 联合诊断
./.venv/bin/python tests/verify_rtmp_e2e.py        # RTMP 端到端（本地 ffmpeg 当服务端）
./tests/verify_unified_rtmp_cli.sh                 # 统一项目 CLI 的 RTMP 全链路
```

## 测试媒体

`media/test_d0.flv` / `test_d300.flv` / `test_d-250.flv` 是**合成**样本：
画面里的“嘴部”区域与声音的说话窗口在**内容上错开 Δ**，但**容器时间戳对齐**——
这正是“OBS 推流前管线”造成的失同步形态。可用 `make_test_media.py` 重新生成：

```bash
./.venv/bin/python tests/make_test_media.py tests/media/test_d300.flv 300
```

## 期望结果

`verify_lip_vs_ts.py`：

| 注入内容偏移 | 时间戳级 | 解码级(ffmpeg) | 内容级（人脸嘴型） | 判定 |
|---|---|---|---|---|
| 0 ms | −36 ms | ~−25 ms | **+6 ms** | PASS |
| +300 ms | −36 ms | ~−22 ms | **−297 ms** | PASS |
| −250 ms | −36 ms | ~−18 ms | **+257 ms** | PASS |

时间戳级/解码级对注入偏移**不敏感**（仅残留 AAC encoder priming 的 ~20–36 ms）＝漏报；
内容级复现误差 ≤7 ms（相关峰 r≈0.97）。

> 时间戳级那 −36 ms 是 AAC 编码器 priming 的系统性残差，不是缺陷；
> 这也是联合诊断对齐容差取 ±45 ms（ITU 可察觉门限）的原因。

## RTMP 端到端说明

`verify_rtmp_e2e.py` 用 ffmpeg 的 `-listen 1` **输出**模式充当“可被读取”的 RTMP 服务端。
该模式一次只服务一个客户端，而工具会顺序建立 2 次连接（探测 + 采样），
因此脚本为每个阶段起一个全新服务端；`verify_unified_rtmp_cli.sh` 则用“循环重启的服务端”
配合适配器的采样重试，跑通完整 CLI。

真实 RTMP 场景请使用 nginx-rtmp / SRS / mediamtx 等多客户端服务端。

## 产物

脚本会在 `tests/.out/`（已忽略提交）下写临时样本与报告。`media/` 若丢失，
用 `make_test_media.py` 重建即可。

## 网页系统验证

```bash
# 需先启动服务： ./start_web.sh 8899
./tests/verify_web_ui.sh        # 解析(京东/淘口令) → 建任务 → 轮询 → 报告 → 历史
```

## 淘口令网关探测（结论可复现）

```bash
./.venv/bin/python tests/probe_taobao_gateway.py
```

若输出 `FAIL_SYS_API_NOT_FOUNDED`（连已知存在的
`mtop.taobao.media.live.room.info` 也一样），说明淘宝 h5 mtop 网关对通用 appKey
关闭了业务 API 白名单 —— 这正是淘口令只能“降级 + 手动指引”的原因。
配置有权限的 appKey/登录态 Cookie 后，`share_resolver._try_taobao_password()` 会自动生效。

## 内容级精度专项

```bash
./.venv/bin/python tests/precision_lip_sync.py     # 50fps 平滑运动 + 非整数帧偏移 → 误差 ≤5ms
./.venv/bin/python tests/probe_xhs_offset.py 14    # 真实小红书间：自动按码率补采长样本并测量
```

`precision_lip_sync.py` 期望输出：三例误差 3~5ms、分辨力 5ms、滑窗一致率 100%。
若误差明显变大，通常是**时间基准（区间中点）**或**细网格插值**被改坏了。

> 经验：内容级测不准的**首要原因不是算法，而是样本时长**。
> 10Mbps 的 1080p 竖屏流，3MB 只有约 2.4 秒 —— 请用 `--lip-min-seconds` 保证 ≥8s。

## FLV 时长估算（时间戳跳变回归）

```bash
./.venv/bin/python tests/unit_flv_duration_jump.py
```

直播 FLV 开头常带一个 `ts=0` 的复位 tag，随后跳到**上行绝对时间**（如 `0 → 8161266 ms`）。
旧实现取「末 tag − 首 tag」，会把 7.4s 的样本算成 **8168s**，导致 `ensure_lip_sample`
误判“样本已够长”而**跳过补采**（`--lip-min-seconds` 失效）→ 唇形分析只拿到几秒。
现改为「相邻 tag 时间戳差 >1s 切段、取最长连续段」，与 `av_sync_detector._longest_segment` 同口径。

> 另：`diagnose_vs_stream_offset()` 现在会在内容级判定为「无法确定/无法评估」时
> **不做跨层归因**，避免出现正文写“无法确定”、联合诊断却笃定“推流前管线失同步”的自相矛盾。

## 内容级可用性增强（人脸后端 + 说话时段）

```bash
./.venv/bin/python tests/unit_face_backend.py     # 关键点 ROI 几何 / 正脸度 / 纹理 / 多脸聚合
./.venv/bin/python tests/unit_voice_activity.py   # 语音活动（VAD-lite）
```

- **人脸后端 `core/face_backend.py`**：优先 **YuNet**（`models/face_detection_yunet_2023mar.onnx`，
  经 `cv2.dnn` 运行，**零新增依赖**；提供 5 点关键点），模型缺失时自动退化 Haar（行为同旧版）。
  关键点 → 紧贴嘴唇的 ROI（取代「人脸框下半部」固定比例），并给出**正脸度**；
  Haar 误检到产品/背景的问题也随之下（实测真实带货样本 Haar 误检帧 100 → YuNet 正确）。
- **语音活动 `core/voice_activity.py`**：纯 numpy 的软指标 v(t)∈[0,1]（语音带占比 × 2–8Hz
  音节率调制），用于「主播是否在说话」判据与片段打分（区分说话 vs BGM）。
- **选段**：`segment_scan` 打分新增「说话（VA）+ 正脸度」两项；窗口字典多了 `frontality` / `env_activity`；
  报告片段行显示 `说话 / 正脸`。
- **门禁**：内容级新增「正脸度 < 0.35 ⇒ 无法确定（主播非正脸）」，仅在关键点可用时生效。

### 高精度通道的 ROI 逐帧跟踪（关键修复）

旧实现把「中位 ROI」当成**固定裁剪框**做高帧率抽帧，只有 ±6px 平移补偿。带货直播里主播
走动/镜头推拉会让嘴部在源画面里移动数百像素（实测某样本中心跨度 **299×636px**，而裁剪框
仅 90×54px）→ 嘴部整体滑出裁剪框。现改为：

1. `_track_geometry`：由逐帧 ROI 求**跟踪中心**与 union 裁剪框（离群帧用 MAD 兜底；
   全量包络接近整帧时才退回内点包络），边距保证取样窗**永远能居中**；
2. `extract_tracked_roi_frames`：按 union 抽帧后，**逐帧把取样窗平移到跟踪中心**；
3. 结果：同一真实样本 `r` 0.185 → **0.203**，滑窗一致率 0 → 0.5（早期版本），
   `roi_tracked` / `track_span_px` / `track_win_clipped` 会写进报告。

### 说话时段门控估计（并列诊断）

`gated_correlation()` + `speaker_gate`：只在语音活动 ≥0.5 的帧上重算互相关，作为**并列估计**
报告（不覆盖全段结论）。实测该样本：全段 r=0.203 → 说话时段（1.83s，占 25%）r=0.239、
偏移 −80.4ms。报告呈现为「【说话时段】…」。

### 说话时段「兜底改判」验证

```bash
./.venv/bin/python tests/verify_speaker_rescue.py
```

构造 12s 信号：前半段=干净语音（嘴动=音频包络平移 40ms），后半段=响但**在另一个滞后上**强相关的
BGM（模拟真实带货直播里把全段带偏的伪相关）。断言：

- 全段稳健估计被污染（−299.6ms，CI 252ms，一致率 0.5）→ 触发门禁；
- 说话子集恢复 **+39.94ms**（r=0.986，CI 0.04ms，一致率 1.0）；
- `speaker_gate_eligible` 达标 → `build_verdict` 输出「内容级同步良好（仅在主播说话时段测得…
  全段不可信）」，`perceptual.content_offset_scope = speaker_subset`；
- 秒数/相关/滑窗/一致率/CI/边界 任一不达标即拒绝兜底。

门控估计用**掩码感知**的多尺度与滑窗（只统计语音覆盖达标的片段），故 CI 才可信。
真实样本若门控子集达不到这些门槛，仍如实判「无法确定」（已验证，不会假改判）。

## 嘴部遮挡（手/物体）判定

```bash
./.venv/bin/python tests/unit_occlusion.py
```

可见性 = 嘴部 ROI 灰度标准差 / 人脸下半部标准差（`face_backend.visibility_series`）。手（同肤色、
低纹理）或物体挡住嘴 → 该值塌陷。判定为**相对本片段自身基线**的阈值
（`occlusion_mask`：`vis < 0.55 × 中位` 且 ≥ 0.20 下限），再叠加「嘴动塌陷但语音仍在」。

> 为何不用绝对阈值：真实美颜/柔光直播的可见性中位只有 ~0.43，用绝对 0.6 会误报 **约一半**帧；
> 改为相对基线后，实测真实房间遮挡占比从 0.48~0.51 降到 **0.003~0.027**（不再误报）。

门禁：遮挡占比 ≥40% 且最长连续 ≥1s → 判定措辞变为「无法确定（嘴部长时间被遮挡：手/物体，占 X%）」；
报告多一行「【嘴部遮挡】…（可见性基线/阈/p10）」；`segment_scan` 打分新增 **遮挡项**（窗口降权），
窗口字典多 `occlusion`。

## 唇几何信号（106 点唇开合）+ 稳健参照 + 跳过门禁

```bash
./.venv/bin/python tests/unit_aperture.py
```

**① 唇几何信号**（`models/2d106det.onnx`，106 点，经 `cv2.dnn`，无新增运行时依赖）：
`face_backend.aperture_series()` 逐帧取嘴部点（实测 0-based **52..71**）→
**唇开合度 = (下半唇均值 y − 上半唇均值 y) / 眼距**。几何量而非纹理量，**不受美颜磨皮影响**。
它作为高精度通道的**第三信号变体**（`raw`/`aligned`/`aperture`）参与竞争，仅当峰相关明显更优
（≥0.03）才采用；`fine.motion_variant` 与 `fine.signal_r` 记录之。

实测（真实样本，帧差 vs 唇几何的峰相关）：

| 样本 | raw(帧差) | aperture(唇几何) |
|---|---|---|
| 抖音带货 1080p | 0.190 | **0.237** |
| B站萌宅（**重美颜**） | 0.073 | **0.179（2.45×）** |
| B站示例直播间（团播） | 0.119 | 0.123 |

> 局限（如实标注）：几何信号的**对照**（眼带纵向跨度）与检测框抖动同源，与唇开合高度共模
> （r 0.19 vs 0.19），**不足以作门禁** → 仅作参考信息（`lip_aperture.gate="informational"`），
> 门禁仍用已验证的图像对照区。

**② 稳健特异性参照**：判据由「对照最大值」改为「对照的 **75 分位**」（`control_r_ref`）；
单点最大易被噪声主导，`control_r_max` 仍保留供查阅。

**③ `--no-specificity` 逃生阀**：`analyze_media(skip_specificity=True)` → `specific=None`+
`skipped=True`，判定里不再出现「特异性不足」（已知干净场景使用，报告标注已跳过）。

### 唇线暗带信号（`mouth_openness_signal`）— 默认基线

动机：美颜磨皮抹平嘴部高频纹理（帧差被压平），但**唇线是强暗边**，平滑后仍存在。
逐帧在嘴部 ROI 取行均值剖面，阈值 = `mean − 0.45·(mean − min)`，统计低于阈值的**行数占比**
（张口时上下唇分离 → 暗带变宽），再减去 0.5s 滑动均值去漂移。

接入方式：它是高精度通道变体竞争的**默认基线**（`raw`/`aligned`/`aperture` 需峰相关高出
≥0.03 才覆盖它），因为它在物理上是最直接的口型代理。

实测（真实样本，峰相关）：

| 样本 | raw(帧差) | aperture(唇几何) | **openness(暗带)** | 采用 |
|---|---|---|---|---|
| 抖音带货 1080p | 0.203 | 0.230 | **0.272** | openness |
| B站萌宅（重美颜） | 0.162 | 0.153 | **0.185** | openness |
| B站示例直播间（团播） | 0.073 | 0.122 | 0.165 | aligned(0.271) |

## 采样端规避（等到「可测」再采样）

```bash
./.venv/bin/python tests/verify_adaptive_capture.py
```

内容级测不准的头号原因是**采到的这段内容不适合**（非正脸 / 嘴被手挡 / 没在说话）。
与其采一段就下结论，不如**持续采样 + 就绪判据**，找到可测窗口再测：

- `readiness_of_window(w)`：正脸度 ≥0.5、遮挡 ≤30%、说话 ≥0.20、嘴动 ≥0.30（缺失指标不否决）。
- `acquire_ready_sample(capture_fn, …)`：每轮采 `chunk_s` 秒 → `scan_windows` 打分 →
  有就绪窗口就**流拷贝**（`export_span_copy`，**不重编码**，保住嘴部高频纹理）截出该窗口返回；
  否则继续，直到 `max_attempts` / `max_seconds` 预算耗尽；仍无则回退历史最佳窗口。
- CLI：`--adaptive-attempts N`（默认 2）、`--adaptive-seconds`（默认 120）、`--adaptive-chunk`（默认 20）。
  仅当首次内容级为「无法确定/无法评估」时触发；规避后若得到确定结论，会用规范 ts 再跑一次
  覆盖证据文件，并把「采样端规避」写进房间指标（出现在报告里）。

测试用**可注入的 capture_fn / scan_fn** 驱动（离线确定），并用真实 ffmpeg 验证流拷贝截取。
真实房间实测（B站 `11223344`）：第 1 轮即找到窗口 `4.56~10.56s`（正脸 0.918、遮挡 0.0、
说话 0.361），内容级嘴部 r 由 0.144 → 0.203；但该房间重美颜，对照区相关同样高 → 仍如实拒判。

## 特异性对照区：人脸框坐标换算（已修的 bug）

```bash
./.venv/bin/python tests/unit_specificity_controls.py
```

**bug**：face-auto 路径把 **`(x,y,w,h)`** 的人脸框直接传给期望 **`(x0,y0,x1,y1)`** 的
`control_rois_from()` → `w/h` 被当成 `x1/y1` → 宽高为负、被 clamp 成 **8×8 像素小噪声块**，
于是「嘴部 r vs 对照最高」的比较对象是 3 个随机小噪声块 → 特异性判据在**所有真实房间运行中
被系统性污染**（手动 ROI 路径不受影响，所以早前的合成验证没暴露它）。

**修复**：新增 `control_rois_from_face(face_xywh, mouth_roi, shape)`，显式换算坐标，对照区
与人脸同尺寸、**不覆盖嘴部**、优先落在**脸外**（左侧/右侧/人脸框下方/上方），找不到才回退通用做法。
回归用例同时锁定「旧写法确实退化成 8×8」与新写法的正确性（同尺寸/不重叠/在画面内）。

> 影响方向因内容而异：修复后 `23756442`（老蒋根雕）特异性由不过 → **通过**（改判为「相关性过低」）；
> 而两个重美颜房因背景同样随音频运动，裕度反而更小。**正确性修复不等于提高通过率**，
> 但比较对象必须是「同尺寸真实区域」才谈得上判据。

## 上传入口（音画同步）验证

> 模型来源：OpenCV Zoo `face_detection_yunet_2023mar.onnx`（Apache-2.0），
> 本环境经 hf-mirror 获取。可用 `LST_FACE_MODEL` 指向自有模型。

## 上传入口（音画同步）验证

```bash
./tests/verify_sync_upload.sh        # 需先启动 ./start_web.sh 8899
```

覆盖：上传页渲染 → multipart 上传 → 检测任务（11s 完成）→ 摘要接口（含 241 点相关曲线与联合诊断）
→ `/uploads/<id>` 的 200/206 Range → 历史按时间混排。

> 踩过的坑（排查时优先看这两条）：
> 1) `do_POST` **不能**对上传路由先做 JSON 预读，否则 multipart body 会被吃光；
> 2) 流式读 multipart 必须按 `Content-Length` 预算 `min(n, remaining)`，
>    否则 `rfile.read(大数)` 会一直阻塞等数据 → 连接挂死（表现为 BrokenPipe / 客户端超时）。

## 抖音分享口令回归

```bash
./.venv/bin/python tests/verify_douyin_share.py     # 依赖外网 + chromium
```

覆盖：识别 `douyin_share` → 短链跳转（`v.douyin.com` → `webcast.amemv.com/.../reflow/<room_id>`）
→ 目标归一 → 适配器解析（房间/主播/标题/档位）。

关键点：抖音直播分享跳到的是 **reflow 页**（不是 `live.douyin.com`），因此
`dy_auth_impl.authenticate_room()` 支持传入 `page_url` 并同时拦截
`/webcast/room/web/enter/` 与 `/webcast/room/reflow/info/` 两种接口。

## 嘴型特异性对照回归

```bash
./.venv/bin/python tests/verify_mouth_specificity.py    # 常驻合成样本，不依赖外网
```

验证“防假阳性”行为：有效嘴部 ROI → 特异性通过并测出 −300ms；无嘴静态 ROI → 判「无法确定」。
商铺直播里把 ROI 移到产品区仍能给出“像样”数值的问题，就是靠这个对照机制拦下来的。

## 健康片段选段

```bash
./.venv/bin/python - <<'PY'
import sys; sys.path[:0] = ['core']
from segment_scan import run_segment_scan
r = run_segment_scan('captures/<样本>.flv', '20260101_000000', 'captures')
print(r['best'], (r.get('measurement') or {}).get('verdict'))
PY
```

产出：`segment_<ts>_clip.mp4`（小播放器）、`segment_<ts>_{face,mouth}_{1..3}.png`（截图）、
`segment_scan_<ts>.json`（打分明细 + 片段测量）。
