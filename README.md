# HomeTV Sources

HomeTV 智能电视版和安卓手机版共同使用的公开频道源。

- `curated.m3u`：设备实际下载的精选频道表。
- `candidates.m3u`：已经过人工初筛的候选及备用线路。
- `manifest.json`：在线版本、更新时间、频道数和 SHA-256。
- `health-report.json`：最近一次自动检测摘要。
- `health-state.json`：连续失败计数；线路连续失败两轮后才会退出发布表。

GitHub Actions 每6小时检查一次候选线路。检测包含播放清单、最高画质变体、真实视频分片、解码信息和下载速度余量。只发布无需账号、Cookie 或私有凭证的公开地址。

在线地址：

- `https://mingfang1188.github.io/HomeTV-Sources/curated.m3u`
- `https://mingfang1188.github.io/HomeTV-Sources/manifest.json`
