# rtmp-h264-stream-trace

**RTMP / RTMPS（H.264）源流体检工具** —— 面向“自己推的流”或任意裸 RTMP 地址的一次性体检，
不依赖任何直播平台接口。**仅供技术研究与自有流排查使用。**

同时以适配器形式集成进统一项目 `live-stream-trace`（`platforms/rtmp.py`），
因此两种用法都可以：

```bash
# ① 独立项目（本目录）
./run.sh rtmp://127.0.0.1:1935/live/streamkey
./run.sh "rtmps://cdn.example.com/app/key?token=xxx" --seconds 25
./run.sh rtmp://a.com/app/key --no-record          # 只探测，不录制样本

# ② 统一项目（得到完整统一报告：档位/播放器/证据链/音画同步）
cd ../live-stream-trace
./run.sh rtmp://127.0.0.1:1935/live/streamkey
```

## 检测项

| 类别 | 内容 |
|------|------|
| 连通性 | 是否可拉流、建连耗时（ffprobe 实测） |
| 视频编码 | **是否 H.264**（HEVC 会告警）、profile/level、像素格式、B 帧 |
| 参数 | 分辨率、帧率、视频/音频/封装码率、实测码率（按样本字节数换算） |
| FLV 结构 | 视频帧/音频帧、关键帧数、**GOP 中位/最大/最小**、A/V 起始偏移 |
| 音轨 | 编码、采样率、声道、码率、AAC AudioSpecificConfig |
| 推流端 | FLV `onMetaData.encoder` → OBS / FFmpeg / Streamlabs…（含版本） |
| 音画同步（可选） | 时间戳级（tag PTS + 解码级 A-V）+ 内容级（人脸嘴型） |

产出：`reports/rtmp_report_<ts>/{index.html,report.txt}` 与 `captures/rtmp_report_<ts>.json`。

## 快速开始

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt
./run.sh rtmp://127.0.0.1:1935/live/streamkey
```

依赖：`ffmpeg` / `ffprobe`（必需，本环境位于
`/opt/ffmpeg/bin/`）。
`requests` 仅用于联动统一项目；内容级嘴型同步另需 `numpy` + `opencv-python-headless`（缺失自动跳过）。

## 播放

浏览器不支持 RTMP，请用 VLC 或 ffplay：

```bash
ffplay rtmp://127.0.0.1:1935/live/streamkey
```

## 本地自测（无真实推流端时）

ffmpeg 的 `-listen 1` **输出**模式可充当一个可被读取的 RTMP 服务端：

```bash
# 终端 A：以服务端方式提供本地文件
ffmpeg -re -stream_loop -1 -i sample.flv -c copy -f flv -listen 1 rtmp://127.0.0.1:1935/live/test
# 终端 B：体检
./run.sh rtmp://127.0.0.1:1935/live/test
```

> 注意：该模式一次只服务一个客户端；工具会先后建立 2 次连接（探测 + 采样），
> 若需要完整流程，请使用真正的 RTMP 服务端（nginx-rtmp / SRS / mediamtx）。

## 参数

| 参数 | 说明 |
|------|------|
| `--seconds N` | 录制样本时长（默认 20s） |
| `--timeout N` | 探测/录制超时（默认 25s） |
| `--no-record` | 只探测不录制 |
| `--no-deep` | 跳过深度音画同步分析 |
| `--report-dir` | 报告输出目录（默认 `reports/`） |
| `--format html\|text\|both` | 报告格式 |

## 判定口径

- **GOP ≤ 4s** 视为正常（直播常规建议 1–2s；过大表示关键帧间隔过长，起播/拖动变慢）。
- **A/V 起始偏移（标签级）** 只反映时间戳层面；若需要“观众实际感知”的错位，
  启用深度分析（内容级人脸嘴型检测可发现推流前采集/编码链引入的失同步）。
- 体检结论分三档：通过 / 基本可用有需关注项 / 存在阻塞性问题。

## 与统一项目的关系

`platforms/rtmp.py` 让统一项目把 RTMP 源流当作一个“平台”，因此自动复用
FLV 元数据解析、推流端指纹、时间戳级 + 内容级音画同步与统一报告；
本目录只提供“短平快”的单流体检与自包含报告。二者的分析口径一致。
