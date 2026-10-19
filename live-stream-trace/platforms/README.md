# 新增平台适配器

每个平台 = `platforms/<platform>.py` 一个文件，导出 `ADAPTER = YourAdapter()`。
core 侧（报告、播放器、音画同步、档位探活、代理）全部自动复用，**无需改动 core**。

## 1. 最小实现

```python
# platforms/example.py
from base import LiveRoom, PlatformAdapter, PlatformMeta, StreamTier
from base import http_probe, ffprobe_url   # 共享探测工具


class ExampleAdapter(PlatformAdapter):
    meta = PlatformMeta(
        key="example",                 # 唯一标识（cookies/<key>.cookie、--platform 取值）
        name="示例平台",
        color="#00AAFF",               # 报告品牌色
        live_url="https://example.com/live/{room_id}",
        cta="前往示例平台",
    )

    def matches(self, text: str) -> bool:
        """识别输入是否属于本平台（URL / 短链 / 房间号）。"""
        return "example.com" in text

    def resolve(self, text: str, ctx: dict | None = None) -> LiveRoom:
        """解析直播间：返回 LiveRoom。ctx 含 cookies / playwright / sample_bytes。"""
        room = LiveRoom(platform="example", room_id="12345")
        room.status = "直播中"
        room.title = "标题"
        room.anchor = "主播"
        room.anchor_id = "uid"
        room.live_start_ts = 1791896217          # 可留空（由采样推断）
        room.metrics.update({"fans": 12345, "viewers": 678})
        room.tiers = [
            StreamTier(label="原画（FLV）", protocol="flv", url="https://cdn/x.flv"),
            StreamTier(label="标清（HLS）", protocol="hls", url="https://cdn/x.m3u8"),
        ]
        room.rtmp = [StreamTier(label="RTMP 源流", protocol="rtmp", url="rtmp://cdn/live/12345")]
        room.push = {"channel": "第三方推流：OBS", "version": "OBS 32.1.0", "third_party": True}
        return room


ADAPTER = ExampleAdapter()
```

`resolve()` 里通常直接调用 `self.probe_tiers(room.tiers)`（或把探活交给 CLI，见下）。

## 2. 归一化数据结构（报告的唯一输入契约）

| 字段 | 说明 |
|------|------|
| `platform` / `room_id` / `status` | 平台标识、房间号、直播状态（"直播中"/"未开播"…） |
| `title` / `anchor` / `anchor_id` / `avatar` | 房间标题、主播名、账号ID、头像 |
| `live_start_ts` / `elapsed_seconds` | 开播时间戳 / 已播秒数（任一有值即可） |
| `metrics` | 账号与房间指标：`fans` `follows` `likes_collects` `viewers` `members` `praise` `pv` `area` `desc` `ip_location` `red_id` `display_id`（缺失即不显示） |
| `tiers` | `list[StreamTier]`：HTTP 档位（FLV / HLS） |
| `rtmp` | `list[StreamTier]`：RTMP 源流（可为空） |
| `push` | `channel` / `version` / `server` / `third_party` |
| `raw` / `notes` | 原始证据（落盘）/ 提示信息（报告中原样展示） |

`StreamTier`：

| 字段 | 说明 |
|------|------|
| `label` | 展示名，如 `原画（H.264/FLV）` |
| `protocol` | `flv` / `hls` / `rtmp` |
| `url` | 拉流地址 |
| `ok` / `status` | 探活结果 / HTTP 状态或错误（CLI 会自动探活并回填） |
| `codec` / `width` / `height` / `bitrate` | 实测参数（探活时回填） |
| `quality_key` | 平台档位键（B 站 `qn10000`、小红书 `source`…），用于地址刷新 |
| `playable` | 网页播放器能否直接播（HEVC / RTMP → False） |
| `note` | 附加说明（如 HEVC 提示） |

## 3. 可选：地址实时刷新（推荐用于短时签名 CDN）

有些平台地址会快速失效（B 站绑定推流会话、约 20~30 分钟）。
实现 `refresh_tier()` 后，`core/serve.py` 的流代理会在播放时实时取新地址：

```python
    def refresh_tier(self, room_id: str, quality_key: str, protocol: str,
                     codec: str = "") -> str | None:
        """返回该档位的新地址；返回 None 表示不刷新（用报告里的地址）。"""
        # 例：按 quality_key（平台的档位键）重新请求接口
        return None
```

## 4. 调试

```bash
# 单独调试适配器
./.venv/bin/python - <<'PY'
import sys; sys.path[:0] = ['core', 'platforms']
from config import platform_registry
room = platform_registry()['example'].resolve('https://example.com/live/12345')
print(room.room_id, room.status, len(room.tiers))
PY

# 全流程
./run.sh https://example.com/live/12345 --format html
```

## 5. 建议的实现路线（用于新平台）

1. **先找数据源**：多数国内平台有三种路线
   - 纯公开接口（最省事，如 B 站 `api.live.bilibili.com`）
   - 页面 SSR（如小红书 `window.__INITIAL_STATE__`，移动端 UA）
   - 需签名/登录（如抖音 `web/enter`、淘宝 mtop）→ 用 Playwright 打开页面**拦截 XHR**（参考 `platforms/dy_auth_impl.py`）
2. **找档位规律**：看接口返回的清晰度列表（B 站 `accept_qn`）或 URL 后缀（小红书 `_orig/_hcv5400/_sd`）；
   逐个探测并用 `ffprobe` 验证编码/分辨率/码率（`base.ffprobe_url`）。
3. **找认证策略**：记录 CDN 是否需要 `Referer`/`Cookie`（用 `http_probe` 对比即可），
   需要 Referer 时在 `core/serve.py` 的 `REFERER_MAP` 里加一条。
4. **账号信息**：优先公开接口；需要登录时用 `cookies/<platform>.cookie` 并在 `notes` 里提示。
