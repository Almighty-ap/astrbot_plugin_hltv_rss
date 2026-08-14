# astrbot_plugin_hltv_rss

HLTV RSS 订阅推送插件(AstrBot + NapCat / OneBot v11)。

## 功能

- 定时轮询 HLTV 官方 RSS 源 `https://www.hltv.org/rss/news`
- 推送 RSS 中的全部 HLTV 资讯(转会、下放、赛事、地图池等),并按内容自动标注分类(签约/加入、下放/替补、离队、综合)
- 使用 AstrBot 已配置的 LLM 把标题和摘要翻译成中文并总结
- 每条新闻只调用一次 LLM,多个订阅会话共用同一翻译结果,不会重复消耗
- 按 `封面图 + 中文标题(附原标题) + AI 总结 + 链接 + 北京时间` 的排版推送
- guid 去重:每条新闻只推送一次;首次安装只记录历史、不刷屏

## 安装

1. 把整个 `astrbot_plugin_hltv_rss` 文件夹复制到 AstrBot 的 `data/plugins/` 目录
2. 在 WebUI 中重启 AstrBot(或热重载插件),依赖 `curl_cffi` 会自动安装

## 前置条件

- NapCat 已通过 OneBot v11 反向 WebSocket 接入 AstrBot
- AstrBot 中至少配置了一个 LLM 提供商(用于翻译总结;没有也能推送,只是不带翻译)

## 指令

| 指令 | 说明 |
| --- | --- |
| `/hltv sub` | 订阅当前会话(群聊/私聊均可) |
| `/hltv unsub` | 取消订阅 |
| `/hltv status` | 查看订阅状态 |
| `/hltv now` | 立即检查一次更新 |
| `/hltv latest` | 取最新一条资讯发到当前会话(测试用) |
| `/hltv help` | 查看帮助 |

## 配置(WebUI → 插件 → HLTV 资讯推送)

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | true | 是否启用定时轮询 |
| `interval_minutes` | 15 | 轮询间隔(分钟) |
| `rss_url` | HLTV 官方源 | RSS 地址 |
| `provider_id` | 空 | 翻译用的 LLM 提供商,留空自动选择 |
| `include_image` | true | 是否附带封面图 |
| `include_link` | true | 文本中是否带 HLTV 原文链接(QQ 屏蔽链接时可关) |
| `message_mode` | image_text | `image_text` 图文直发 / `forward` 合并转发 |
| `use_proxy` | false | 抓取 HLTV(RSS 与封面图)时是否使用代理 |
| `proxy_url` | 空 | 代理地址,如 `http://127.0.0.1:7890`,仅代理开关开启时生效 |

### 关于代理

- HLTV 在 Cloudflare 后面,会检测 TLS 指纹(JA3):Python aiohttp 的指纹即使走代理也会被 403,所以插件改用 `curl_cffi` 伪装 Chrome 指纹抓取(RSS 和封面图都是)。
- 默认关闭代理。如果伪装 Chrome 指纹后仍被拦(IP 被 Cloudflare 标记),有两种解决办法:
  1. 打开 `use_proxy` 并填写 `proxy_url`;
  2. 或者给容器设置 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量,插件会自动沿用(与 curl 行为一致)。
- 显式填写的 `proxy_url` 优先于环境变量。
- Docker 内 `127.0.0.1` 指向容器自身,若代理在宿主机上,请填写宿主机 IP(如 `http://172.17.0.1:7890`)或代理容器名。

## 说明

- 新闻时间自动从 GMT 换算为北京时间(UTC+8)
- 封面图由插件在 AstrBot 内下载(Chrome 指纹 + 代理),以 base64 发送,不依赖 NapCat 的网络;下载失败自动退化为纯文本推送
- 代理同时作用于 RSS 抓取和封面图下载
- 拉取报 403 时,指令回复会附带代理配置提示
- QQ 可能屏蔽 hltv.org 链接:可关闭 `include_link`,或把 `message_mode` 切为 `forward` 合并转发
- 全量推送:RSS 中每条新资讯都会触发一次 LLM 翻译总结(无论订阅了多少个会话,每条新闻只翻译一次),请关注 LLM 用量与费用
