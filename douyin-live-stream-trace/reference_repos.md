# 抖音直播技术参考资料汇总

> 自动整理于 2026-05-16，按类别分组，含 GitHub 仓库、博客文章、商业工具

---

## 一、直播录制工具

| 仓库/资源 | Stars | 语言 | 说明 | 最近更新 |
|-----------|-------|------|------|---------|
| [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) | ~9,700 | Python | 多平台直播录制（40+平台），FFmpeg驱动，自定义画质 | 2025-01 |
| [LyzenX/DouyinLiveRecorder](https://github.com/LyzenX/DouyinLiveRecorder) | ~878 | Python | GUI/命令行，弹幕录制，自动cookie获取 | 2025-09 |
| [bililive-go/bililive-go](https://github.com/bililive-go/bililive-go) | ~5,503 | Go | 多平台录制，含抖音，自动管理 | 2026-04 |
| [dragons96/DouyinLiveRecorder-webui](https://github.com/dragons96/DouyinLiveRecorder-webui) | - | Web | DouyinLiveRecorder 的 Web 管理系统 | 2025-04 |
| [sqing33/Docker.DouyinLiveRecorder-webui](https://github.com/sqing33/Docker.DouyinLiveRecorder-webui) | - | Python+Web | Docker化 WebUI 版录制管理 | 2025-05 |
| [ihmily/streamget](https://github.com/ihmily/streamget) | 177 | Python | 多平台直播流解析库（HLS/FLV，40+平台） | 活跃 |

## 二、弹幕与消息流抓取

| 仓库/资源 | Stars | 语言 | 说明 | 最近更新 |
|-----------|-------|------|------|---------|
| [jwwsjlm/douyinLive](https://github.com/jwwsjlm/douyinLive) | ~281 | Go | WebSocket弹幕抓取/转发，支持礼物/点赞/进场 | 2024-05 |
| [isaackogan/TikTokLive](https://github.com/isaackogan/TikTokLive) | ~1,396 | Python | TikTok直播事件接收库（评论/礼物/点赞） | 2026-02 |
| [zerodytrash/TikTok-Live-Connector](https://github.com/zerodytrash/TikTok-Live-Connector) | ~2,000 | TypeScript | Node.js TikTok Webcast连接库 | 2026-05 |
| [isaackogan/TikTok-Webcast-Protobuf](https://github.com/isaackogan/TikTok-Webcast-Protobuf) | - | Proto | TikTok LIVE Protobuf Schema（clean-room 协议还原） | 2026-04 |
| [jwwsjlm/TikTok_Live](https://github.com/jwwsjlm/TikTok_Live) | ~30 | Go | TikTok直播弹幕抓取（WebSocket） | 2026-02 |
| [zhonghangAlex/DySpider](https://github.com/zhonghangAlex/DySpider) | ~298 | Python | 完整WebSocket+Protobuf实现 | 活跃 |
| [XiaoMiku01/douyin-live-go](https://github.com/XiaoMiku01/douyin-live-go) | ~151 | Go | Go语言弹幕爬虫，完整protobuf处理 | 活跃 |
| [cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) | ~1,219 | JS/Python | 抖音爬虫+直播间监听（弹幕/礼物/私信） | 2026-01 |

## 三、Protobuf 定义与协议文档

| 仓库/资源 | Stars | 语言 | 说明 | 最近更新 |
|-----------|-------|------|------|---------|
| [freeloop4032/douyin-live](https://github.com/freeloop4032/douyin-live) | ~97 | Python/JS | dy.proto + WebSocket连接 + 消息解析 | 2022-12 |
| [Remember-the-past/douyin_proto](https://github.com/Remember-the-past/douyin_proto) | ~95 | Proto | method与proto对应关系文档，持续更新 | 活跃 |
| [chenggx/douyin-parse-danmu](https://github.com/chenggx/douyin-parse-danmu) | - | Python | 弹幕解析，完整proto定义+实现 | - |

### WebSocket 消息流程

```
1. 建立 WSS 连接 (wss://webcast3-ws-web-lq.douyin.com/webcast/im/push/v2/...)
2. 接收 PushFrame (gzip压缩)
3. 解压得到 Response
4. 根据 method 解析具体消息类型
5. 发送 ACK 应答 (needAck=true时)
6. 定期心跳包
```

### 关键 Proto 消息结构

```
PushFrame → Response → Message → 具体消息类型:
  ├─ WebcastChatMessage (弹幕)
  ├─ WebcastGiftMessage (礼物)
  ├─ WebcastMemberMessage (入场)
  ├─ WebcastLikeMessage (点赞)
  ├─ WebcastSocialMessage (关注)
  └─ WebcastRoomStatsMessage (统计)
```

## 四、签名算法研究

### a_bogus / X-Bogus / msToken

| 仓库/资源 | 类型 | 说明 |
|-----------|------|------|
| [jackluson/a_bogus_douyin](https://github.com/jackluson/a_bogus_douyin) | Repo | a_bogus参数生成，在线API服务 |
| [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) (~17,440⭐) | Repo | 含完整 a_bogus Python实现 (crawlers/douyin/web/abogus.py) |
| [CHEN-HONGLIN/bytedance_tiktok](https://github.com/CHEN-HONGLIN/bytedance_tiktok) | Repo/文档 | X-Argus/X-Gorgon/X-Bogus/X-Ladon/X-Typhon/X-Medusa详解 |
| [int4444/tiktok-api](https://github.com/src244/tiktok_reverse) (~233⭐) | Repo | 移动端+Web端签名（SM3/SIMON/AES/RC4） |
| [justscrapeme/tiktok-web-reverse-engineering](https://github.com/justscrapeme/tiktok-web-reverse-engineering) (~141⭐) | Repo | X-Gnarly实现与TikTok Web签名 |
| [notemrovsky/tiktok-reverse-engineering](https://github.com/notemrovsky/tiktok-reverse-engineering) (~130⭐) | Repo | TikTok JS VM 协议分析（77操作码映射+字节码反汇编） |
| [justbeluga/tiktok-web-reverse-engineering](https://github.com/justbeluga/tiktok-web-reverse-engineering) (~54⭐) | Repo | X-Bogus & X-Gnarly加解密 |
| [getdou/dou](https://github.com/getdou/dou) | Repo | 开源抖音API，含gateway/auth.py |

### 签名算法要点

- **a_bogus**: 新版签名，通过 bdms（字节安全SDK）生成，需模拟浏览器环境
- **msToken**: 消息令牌，~7天过期，可从Cookie获取或随机生成107位字符串
- **X-Bogus** (旧版): MD5 + RC4 + 自定义Base64多阶段加密
- **X-Argus**: SM3哈希，更复杂的签名

### 签名相关博客

| 文章 | URL | 要点 |
|------|-----|------|
| 道满PythonAI | https://www.daomanpy.com/ | 抖音 a_bogus 参数加密分析 |
| 技术栈 | https://jishuzhan.net/article/2036604335254208514 | XHR断点调试a_bogus |
| 博客园 chriski | https://www.cnblogs.com/dy9527/p/19057109 | a_bogus+mstoken 全参数分析 2024 |
| CSDN | https://blog.csdn.net/2403_87731058/article/details/143744659 | X-Bogus深度分析 |
| 0en.cn | https://www.0en.cn/archives/210.html | X-Bogus研究实战案例 |
| doupoa.site | https://doupoa.site/archives/506 | Playwright 自动化签名处理 |

## 五、FLV/RTMP/AMF 分析工具

### FLV 解析

| 仓库/资源 | Stars | 语言 | 说明 |
|-----------|-------|------|------|
| [wulczer/flvlib](https://github.com/wulczer/flvlib) | 24 | Python | FLV解析、验证、索引 |
| [zkonge/flvlib3](https://github.com/zkonge/flvlib3) | 2 | Python | flvlib的Python3版 |
| [chrippa/python-flashmedia](https://github.com/chrippa/python-flashmedia) | 18 | Python | FLV+AMF0/AMF3解析，无损序列化 |
| [chemag/flvparser](https://github.com/chemag/flvparser) | - | C++ | 纯C++ FLV解析器 |
| [goldvideo/demuxer](https://github.com/goldvideo/demuxer) | - | TypeScript | 视频解复用器，含FLV AMF |

### AMF0/AMF3 库

| 仓库/资源 | Stars | 语言 | 说明 |
|-----------|-------|------|------|
| [StdCarrot/Py3AMF](https://github.com/StdCarrot/Py3AMF) | 50 | Python | PyAMF的Python3分支，AMF0/AMF3 |
| [hydralabs/pyamf](https://github.com/hydralabs/pyamf) | - | Python | AMF0/AMF3原始实现 |
| [StounhandJ/go-amf](https://github.com/StounhandJ/go-amf) | - | Go | AMF0/AMF3编解码 |
| [ProjectCryo/AMF.js](https://github.com/ProjectCryo/AMF.js/) | - | JS | Node.js AMF0/AMF3编解码 |
| [mtanksl/amf.net](https://github.com/mtanksl/amf.net) | 21 | C# | AMF0/AMF3 .NET实现 |

### RTMP 分析

| 仓库/资源 | Stars | 语言 | 说明 |
|-----------|-------|------|------|
| [gyk/wireshark-rtmp-dissector](https://github.com/gyk/wireshark-rtmp-dissector) | 1 | C | Wireshark RTMP协议分析插件 |
| [andreafabrizi/rtmpSnoop](https://github.com/andreafabrizi/rtmpSnoop) | - | Python | RTMP嗅探，PCAP读取+属性转储 |
| [illuspas/tsdebugger](https://github.com/illuspas/tsdebugger) | - | Go | RTMP/FLV流时间戳调试 |
| [ZhaoliangGuo/StreamingMediaToolKit](https://github.com/ZhaoliangGuo/StreamingMediaToolKit) | - | - | RTMP流分析+时间戳+录制 |
| [Eittipat/pyrtmp](https://github.com/Eittipat/pyrtmp) | - | Python | Python RTMP实现（FLV+AMF） |

## 六、协议解析博客文章

| 文章 | URL | 要点 |
|------|-----|------|
| lxspider | http://www.lxspider.com/?p=166 | 抖音web直播protobuf协议解析 |
| CSDN 弹幕解析 | https://blog.csdn.net/qq_38223858/article/details/146268792 | WebSocket+Protobuf+ACK机制详解 |
| CSDN 协议记录 | https://blog.csdn.net/Ecci_/article/details/125533375 | 弹幕获取过程，gzip解压分析 |
| 腾讯云 | https://cloud.tencent.com/developer/article/2530112 | 抖音弹幕wss协议工具 |
| Artiely Blog | http://artiely.com/posts/2022/2022-12-20-ws/index.html | WebSocket 协议分析各平台弹幕方案 |
| ruesin.cn | https://notes.ruesin.cn/app/douyin-web.html | signature 算法分析，webpack 分析 |
| addy.vip | https://addy.vip/p/1029/ | X-Bogus参数分析PHP/Python示例 |

## 七、商业/托管服务

| 服务 | URL | 说明 |
|------|-----|------|
| 抖音开放平台 | https://developer.open-douyin.com/docs/resource/zh-CN/dop/ability/douyin-live-sdk | 官方直播接入文档 |
| Euler Stream | https://eulerstream.com/docs/signatures | TikTok签名商业服务 |
| TikTool | https://docs.tik.tools/ | TikTok LIVE API托管 |
| tiktool/tiktok-live-api | https://github.com/tiktool/tiktok-live-api | 托管API客户端 |
| StreamLake | https://www.streamlake.com/product/live | 字节直播云服务 |
| 爱复盘 | https://ifupan.com/home | 直播AI复盘 |
| 播动师 | https://www.bodongshi.com/ | 抖音直播数据分析 |
| 抖音灵机 | https://ai.ipkd.cn/sites/livedata.html | 直播数据监控+复盘 |

## 八、其他相关工具

| 仓库/资源 | Stars | 语言 | 说明 |
|-----------|-------|------|------|
| [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) | 大量 | Python | 视频下载，含FLV读取器 |
| [ruffle-rs/ruffle](https://github.com/ruffle-rs/ruffle) | 大量 | Rust | Flash Player模拟器，含FLV解析 |
| [harlanc/xiu](https://github.com/harlanc/xiu) | - | Rust | FLV转opus等处理 |
| [biliup/biliup](https://github.com/biliup/biliup) | - | Rust/Python | B站上传工具，含FLV解析 |
| [liuchuancong/pure_live](https://github.com/liuchuancong/pure_live) | - | Dart | 多平台直播聚合（含抖音） |
| [JorenSix/Olaf](https://github.com/JorenSix/Olaf) | 388 | C/Rust | 轻量声学指纹系统 |

---

## 优先参考建议

1. **Protobuf更新**: `Remember-the-past/douyin_proto`（持续更新）+ `freeloop4032/douyin-live`
2. **弹幕实现**: `zhonghangAlex/DySpider`（Python完整实现）+ `XiaoMiku01/douyin-live-go`（Go）
3. **签名算法**: `Evil0ctal/Douyin_TikTok_Download_API` 的 abogus.py + `CHEN-HONGLIN/bytedance_tiktok`
4. **FLV/AMF解析**: `chrippa/python-flashmedia` + `StdCarrot/Py3AMF`
5. **RTMP分析**: Wireshark原生RTMP dissector + `andreafabrizi/rtmpSnoop`
