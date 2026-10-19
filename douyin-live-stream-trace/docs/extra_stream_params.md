# 额外流参数说明

通过观察 URL 中发现的额外参数，API 返回的流地址不包含这些参数。
通过 `mappings/extra_stream_params.json` 配置启用/禁用。

## 参数列表

| 参数名 | 默认值 | 作用域 | 来源 | 用途推测 |
|--------|--------|--------|------|----------|
| `only_audio` | `1` | audio_only | 服务端协议 | 请求纯音频流（无视频轨） |
| `eupf_relay_mode` | `err` | audio_only | 服务端协议 | EUPF 中继模式，配合 only_audio |
| `rtm_expr_tag` | `android_rtm_online_wifi_latency_opt` | audio_only | Android 客户端 | RTM 延迟优化实验（WiFi 低延迟） |
| `abr_expr` | `4319727_v4` | all | Android 客户端 | 码率自适应实验版本标记 |
| `abr_pts` | `-500` | all | Android 客户端 | 码率策略偏移（负值倾向更低码率） |
| `en_abs` | `1` | all | Android 客户端 | 启用 A/B 实验开关 |
| `m_p2p` | `v1` | all | Android 客户端 | P2P 传输功能标记 |
| `fast_p2p` | `v1` | all | Android 客户端 | 快速 P2P 模式 |
| `noon_fp2p` | `v3` | all | Android 客户端 | 非 P2P 回退策略版本 |
| `box_h2p` | `v1` | all | Android 客户端 | HTTP/2 推送协议 |
| `new_liveplayer` | `v1` | all | Android 客户端 | 新版直播播放器标记 |
| `pt` | `v1` | all | Android 客户端 | 播放类型标记 |
| `ptag` | `v1` | all | Android 客户端 | 播放标签 |
| `fallback_type` | `2` | all | Android 客户端 | CDN/协议回退策略 |
| `mxipstack` | `v2` | all | Android 客户端 | IP 协议栈版本（IPv4/IPv6 双栈） |

## 作用域说明

- **audio_only**: 仅附加到纯音频流 URL（`build_audio_only_url()`）
- **all**: 当使用 `--extra-params` 参数时，附加到所有清晰度的流 URL

## 配置方式

编辑 `mappings/extra_stream_params.json`：
- `"enabled": true` — 启用该参数
- `"enabled": false` — 禁用该参数
- `scope` 控制参数在哪些场景下生效

## 使用方式

```bash
# 默认：只附加 audio_only 作用域的参数（用于纯音频流）
python analyze_room.py --room 64668142235 --playwright --format both --capture-seconds 5

# 附加所有启用的参数到全部流
python analyze_room.py --room 64668142235 --playwright --format both --capture-seconds 5 --extra-params
```

## API 返回 URL 已有的参数（无需配置）

以下参数由房间 API 返回，自动包含在流地址中：
- `expire` — URL 过期时间
- `sign` — 签名
- `major_anchor_level` — 主播等级
- `arch_hrchy` — 架构层级
- `exp_hrchy` — 实验层级
- `neq` — 未知标记
- `unique_id` — 流唯一标识
- `t_id` — 事务 ID
