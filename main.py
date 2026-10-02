"""
HLTV RSS 订阅推送 - AstrBot 插件

订阅 HLTV 官方 RSS 源(https://www.hltv.org/rss/news),
自动抓取 HLTV 全部 CS 资讯(转会、下放、赛事、地图池等),
使用 LLM 翻译总结后,按 "封面图 + 中文标题 + AI 总结 + 链接 + 时间" 的格式
推送到订阅的群聊/私聊。适配 NapCat(OneBot v11)。
"""

import asyncio
import email.utils
import html
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from curl_cffi.requests import AsyncSession

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

RSS_URL_DEFAULT = "https://www.hltv.org/rss/news"
CST = timezone(timedelta(hours=8))  # 北京时间
MAX_SEEN = 500  # 最多记住多少条历史 guid 用于去重
MEDIA_NS = "{http://search.yahoo.com/mrss/}"

# Cloudflare 会按 TLS/JA3 指纹拦截,而不同出口 IP(代理节点)能被接受的指纹不一样:
# 实测同一节点下 curl 200、curl_cffi 的 chrome 指纹 403。这里按顺序逐个尝试,
# 命中后记住,后续优先复用。
IMPERSONATE_CANDIDATES = ["firefox", "edge", "chrome_android", "chrome"]
PLUGIN_VERSION = "1.4.0"

# 命中规则 -> 推送消息中的分类标签
CATEGORY_RULES = [
    (r"\b(bench(?:es|ed)?|demot(?:e|es|ed)|stand-?ins?)\b", "🪑 下放/替补"),
    (r"\b(leave|leaves|left|departs?|parts? ways|releas(?:e|es|ed))\b", "👋 离队"),
    (
        r"\b(sign(?:s|ed|ing)?|join(?:s|ed)?|acquir(?:e|es|ed)|"
        r"promot(?:e|es|ed)|returns?|completes? the roster)\b",
        "✍️ 签约/加入",
    ),
]

TRANSLATE_PROMPT = (
    "你是 CS 电竞资讯翻译助手。请把下面的 HLTV 新闻标题和摘要翻译成自然流畅的中文,"
    "并做简要总结。\n"
    "输出格式(严格遵守):\n"
    "标题:<中文标题>\n"
    "总结:<2~3 句中文总结>\n"
    "要求:\n"
    "- 选手 ID、战队名、赛事名保留英文原文\n"
    "- 不要编造原文没有的信息\n"
    "- 不要输出任何多余内容\n\n"
    "新闻标题:{title}\n"
    "新闻摘要:{description}"
)

HELP_TEXT = (
    "📰 HLTV 资讯订阅\n"
    "━━━━━━━━━━━━━━━━\n"
    "/hltv sub — 订阅当前会话\n"
    "/hltv unsub — 取消订阅\n"
    "/hltv status — 查看订阅状态\n"
    "/hltv now — 立即检查更新\n"
    "/hltv latest — 取最新一条资讯发到本会话\n"
    "━━━━━━━━━━━━━━━━\n"
    "自动推送 HLTV 全部资讯,\n"
    "附带 LLM 中文翻译总结与封面图"
)


@dataclass
class NewsItem:
    """一条 HLTV RSS 新闻"""

    guid: str
    title: str
    description: str
    link: str
    image_url: str
    pub_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _node_text(node: ET.Element, tag: str) -> str:
    element = node.find(tag)
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _clean_text(text: str) -> str:
    """去掉 HTML 标签、解码实体、压缩空白。"""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(xml_text: str) -> List[NewsItem]:
    """解析 HLTV RSS,返回新闻列表(保持源内顺序)。"""
    items: List[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error("[HLTV RSS] RSS 解析失败:%s", e)
        return items

    for node in root.iter("item"):
        guid = _node_text(node, "guid") or _node_text(node, "link")
        if not guid:
            continue
        link = _node_text(node, "link")
        title = _clean_text(_node_text(node, "title"))
        description = _clean_text(_node_text(node, "description"))
        image_url = ""
        media = node.find(f"{MEDIA_NS}content")
        if media is not None:
            image_url = (media.get("url") or "").strip()
        pub_time: Optional[datetime] = None
        raw_date = _node_text(node, "pubDate")
        if raw_date:
            try:
                pub_time = email.utils.parsedate_to_datetime(raw_date)
            except Exception:
                pub_time = None
        items.append(
            NewsItem(
                guid=guid,
                title=title,
                description=description,
                link=link,
                image_url=image_url,
                pub_time=pub_time,
            )
        )
    return items


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class HltvRssPlugin(Star):
    """HLTV RSS 订阅推送"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._working_impersonate: Optional[str] = None
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "[HLTV RSS] 插件版本 v%s,指纹回退链 %s",
            PLUGIN_VERSION,
            " -> ".join(IMPERSONATE_CANDIDATES),
        )
        logger.info(
            "[HLTV RSS] 插件已启动,轮询间隔 %s 分钟", self._interval_minutes()
        )

    async def terminate(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except Exception:
                pass

    # ------------------------- 配置读取 -------------------------

    def _interval_minutes(self) -> int:
        try:
            return max(1, int(self.config.get("interval_minutes") or 15))
        except (TypeError, ValueError):
            return 15

    def _rss_url(self) -> str:
        return (self.config.get("rss_url") or "").strip() or RSS_URL_DEFAULT

    def _proxy_url(self) -> Optional[str]:
        """代理地址:显式配置优先;未配置时回退容器的 HTTP(S)_PROXY 环境变量。"""
        if bool(self.config.get("use_proxy", False)):
            proxy = str(self.config.get("proxy_url") or "").strip()
            if proxy:
                return proxy
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = os.environ.get(key, "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _fetch_error_hint(error: Exception) -> str:
        text = str(error)
        if "403" in text or "407" in text:
            return (
                "\n(403/407:所有浏览器指纹都被 Cloudflare 拒绝。请确认已开启 use_proxy "
                "并填写可用的 proxy_url(或为容器设置 HTTP_PROXY/HTTPS_PROXY);"
                "插件会按 firefox -> edge -> chrome_android -> chrome 自动重试,"
                "也可在插件设置里手动指定 impersonate;若仍失败,说明当前代理出口 IP "
                "被 Cloudflare 标记,换个节点/线路)"
            )
        return ""

    # ------------------------- KV 存储 -------------------------

    async def _get_subscribers(self) -> List[str]:
        return list(await self.get_kv_data("subscribers", []) or [])

    async def _save_subscribers(self, subscribers: List[str]):
        await self.put_kv_data("subscribers", list(subscribers))

    # ------------------------- 轮询 -------------------------

    async def _poll_loop(self):
        await asyncio.sleep(10)  # 等待 AstrBot 启动、平台连接完成
        while True:
            if bool(self.config.get("enable", True)):
                try:
                    await self._check_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "[HLTV RSS] 轮询出错:%s%s", e, self._fetch_error_hint(e)
                    )
            await asyncio.sleep(self._interval_minutes() * 60)

    async def _check_once(self) -> List[NewsItem]:
        """抓取一次 RSS,把全部新资讯推送给订阅者。返回本次新资讯列表。"""
        xml_text = await self._fetch_rss()
        items = parse_rss(xml_text)
        if not items:
            return []

        seen = list(await self.get_kv_data("seen_guids", []) or [])
        seen_set = set(seen)
        initialized = bool(await self.get_kv_data("initialized", False))
        new_items = [item for item in items if item.guid not in seen_set]

        if not initialized:
            # 首次运行只记录历史,不推送,避免刷屏
            await self.put_kv_data(
                "seen_guids", [item.guid for item in items][-MAX_SEEN:]
            )
            await self.put_kv_data("initialized", True)
            logger.info(
                "[HLTV RSS] 首次运行,已记录 %s 条历史资讯,不推送", len(items)
            )
            return []

        if new_items:
            merged = (seen + [item.guid for item in new_items])[-MAX_SEEN:]
            await self.put_kv_data("seen_guids", merged)

        if not new_items:
            return []

        # 按发布时间从旧到新排序,依次推送全部新资讯
        new_items.sort(
            key=lambda item: item.pub_time or datetime.min.replace(tzinfo=timezone.utc)
        )

        subscribers = await self._get_subscribers()
        if not subscribers:
            logger.info(
                "[HLTV RSS] 有 %s 条新资讯,但没有订阅会话,跳过推送"
                "(发送 /hltv sub 订阅)",
                len(new_items),
            )
            return new_items

        for item in new_items:
            await self._process_and_push(item, subscribers)
        return new_items

    async def _process_and_push(self, item: NewsItem, subscribers: List[str]):
        llm_text = ""
        try:
            llm_text = await self._llm_translate(item)
        except Exception as e:
            logger.warning("[HLTV RSS] LLM 翻译失败(%s):%s", item.title, e)

        image_bytes = await self._prepare_image(item)
        chain, text_only_chain = self._build_chains(item, llm_text, image_bytes)
        for umo in subscribers:
            try:
                await self.context.send_message(umo, chain)
            except Exception as e:
                # 封面图下载失败等情况,退化为纯文本重发
                logger.warning("[HLTV RSS] 推送失败(%s),尝试纯文本重发:%s", umo, e)
                try:
                    await self.context.send_message(umo, text_only_chain)
                except Exception as e2:
                    logger.error("[HLTV RSS] 纯文本重发仍失败(%s):%s", umo, e2)

    # ------------------------- RSS / LLM -------------------------

    def _impersonate_candidates(self) -> List[str]:
        """指纹候选:已探明可用的排最前,失败后回到完整回退链。"""
        configured = str(self.config.get("impersonate") or "auto").strip().lower()
        if configured and configured != "auto":
            return [configured]
        if self._working_impersonate:
            return [self._working_impersonate] + [
                p for p in IMPERSONATE_CANDIDATES if p != self._working_impersonate
            ]
        return list(IMPERSONATE_CANDIDATES)

    async def _request_with_fallback(self, url: str, headers: Optional[dict] = None):
        """按指纹回退链请求 url,返回成功(HTTP < 400)的响应。

        Cloudflare 按 TLS/JA3 指纹拦截时,同一个出口 IP 下 curl 能过、
        curl_cffi 的 chrome 指纹也可能被 403,所以逐个指纹试,谁先成功就记住谁。
        """
        proxy = self._proxy_url()
        candidates = self._impersonate_candidates()
        last_error: Optional[Exception] = None
        for index, profile in enumerate(candidates):
            try:
                async with AsyncSession(
                    impersonate=profile, proxy=proxy, timeout=30
                ) as session:
                    resp = await session.get(url, headers=headers)
                if resp.status_code >= 400:
                    last_error = RuntimeError(
                        "HTTP %s(指纹 %s)" % (resp.status_code, profile)
                    )
                    logger.info(
                        "[HLTV RSS] 指纹 %s 被拒(HTTP %s)%s",
                        profile,
                        resp.status_code,
                        ",继续尝试下一个指纹"
                        if index + 1 < len(candidates)
                        else "",
                    )
                    continue
                if self._working_impersonate != profile:
                    self._working_impersonate = profile
                    logger.info("[HLTV RSS] 使用指纹 %s 抓取成功", profile)
                return resp
            except Exception as e:
                last_error = e
                logger.info("[HLTV RSS] 指纹 %s 请求异常:%s", profile, e)
        if last_error is not None:
            raise last_error
        raise RuntimeError("没有可用的抓取指纹")

    async def _fetch_rss(self) -> str:
        headers = {
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }
        resp = await self._request_with_fallback(self._rss_url(), headers)
        return resp.text

    async def _download_image(self, url: str) -> Optional[bytes]:
        """封面图同样经过 Cloudflare CDN,复用同一套指纹与代理在插件内下载。"""
        try:
            resp = await self._request_with_fallback(url)
            return resp.content
        except Exception as e:
            logger.warning("[HLTV RSS] 封面图下载失败(%s):%s", url, e)
            return None

    async def _prepare_image(self, item: NewsItem) -> Optional[bytes]:
        """按配置下载封面图,失败或关闭时返回 None(退化为纯文本)。"""
        if not bool(self.config.get("include_image", True)):
            return None
        if not item.image_url:
            return None
        return await self._download_image(item.image_url)

    async def _llm_translate(self, item: NewsItem) -> str:
        provider_id = await self._resolve_provider_id()
        if not provider_id:
            logger.warning("[HLTV RSS] 未找到可用 LLM 提供商,本次推送不带翻译")
            return ""
        prompt = TRANSLATE_PROMPT.format(
            title=item.title, description=item.description or "(无)"
        )
        resp = await self.context.llm_generate(
            chat_provider_id=provider_id, prompt=prompt
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _resolve_provider_id(self) -> Optional[str]:
        """LLM 提供商优先级:插件配置 > 订阅会话当前模型 > 任意可用聊天模型。"""
        provider_id = str(self.config.get("provider_id") or "").strip()
        if provider_id:
            return provider_id
        for umo in await self._get_subscribers():
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                if provider_id:
                    return provider_id
            except Exception:
                continue
        try:
            manager = getattr(self.context, "provider_manager", None)
            instances = getattr(manager, "provider_insts", None) if manager else None
            if callable(instances):
                instances = instances()
            if isinstance(instances, dict):
                for pid, provider in instances.items():
                    meta = getattr(provider, "meta", None)
                    provider_type = getattr(meta, "type", None)
                    if provider_type in (None, "chat"):
                        return pid
        except Exception as e:
            logger.debug("[HLTV RSS] 枚举 LLM 提供商失败:%s", e)
        return None

    # ------------------------- 消息构建 -------------------------

    def _build_chains(
        self,
        item: NewsItem,
        llm_text: str,
        image_bytes: Optional[bytes] = None,
    ) -> Tuple[MessageChain, MessageChain]:
        """返回 (完整消息链, 纯文本消息链)。

        包装成 MessageChain 对象:context.send_message 主动发送时只接受
        MessageChain(带 .chain 属性),直接传 list 会报
        "'list' object has no attribute 'chain'"。
        """
        zh_title, summary = self._parse_llm_output(llm_text)

        lines = [f"📰 HLTV 资讯速递 · {self._category(item)}", ""]
        display_title = zh_title or item.title
        lines.append(f"【{display_title}】")
        if zh_title and zh_title.strip() != item.title.strip():
            lines.append(f"({item.title})")
        lines.append("")
        lines.append(summary or item.description or "(无摘要)")
        lines.append("")
        if bool(self.config.get("include_link", True)):
            lines.append(f"🔗 {item.link}")
        lines.append(f"🕐 {self._format_time(item.pub_time)}")
        text = "\n".join(lines)

        text_only_chain = MessageChain(chain=[Comp.Plain(text)])
        mode = str(self.config.get("message_mode") or "image_text").strip()
        if mode == "forward":
            # 合并转发模式:封面图与正文作为节点放进一条转发消息
            nodes = []
            if image_bytes:
                nodes.append(
                    Comp.Node(
                        content=[Comp.Image.fromBytes(image_bytes)],
                        name="HLTV 资讯速递",
                    )
                )
            nodes.append(
                Comp.Node(content=[Comp.Plain(text)], name="HLTV 资讯速递")
            )
            chain = MessageChain(chain=[Comp.Nodes(nodes=nodes)])
        elif image_bytes:
            chain = MessageChain(
                chain=[Comp.Image.fromBytes(image_bytes), Comp.Plain(text)]
            )
        else:
            chain = text_only_chain
        return chain, text_only_chain

    def _category(self, item: NewsItem) -> str:
        text = f"{item.title} {item.description}".lower()
        for pattern, label in CATEGORY_RULES:
            if re.search(pattern, text):
                return label
        return "📄 综合"

    @staticmethod
    def _parse_llm_output(text: str) -> Tuple[str, str]:
        """把 LLM 输出拆成 (中文标题, 总结)。解析失败时整段作为总结。"""
        if not text:
            return "", ""
        title_match = re.search(r"标题[:：]\s*(.+)", text)
        summary_match = re.search(r"总结[:：]\s*([\s\S]+)", text)
        zh_title = title_match.group(1).strip() if title_match else ""
        summary = summary_match.group(1).strip() if summary_match else text.strip()
        return zh_title, summary

    @staticmethod
    def _format_time(pub_time: Optional[datetime]) -> str:
        if pub_time is None:
            return "时间未知"
        if pub_time.tzinfo is None:
            pub_time = pub_time.replace(tzinfo=timezone.utc)
        return pub_time.astimezone(CST).strftime("%Y-%m-%d %H:%M") + " 北京时间"

    # ------------------------- 指令 -------------------------

    @staticmethod
    def _parse_args(event: AstrMessageEvent) -> List[str]:
        text = (event.message_str or "").strip()
        parts = text.split()
        if parts and parts[0].lstrip("/").lower() == "hltv":
            parts = parts[1:]
        return parts

    @filter.command("hltv")
    async def hltv_command(self, event: AstrMessageEvent):
        """HLTV 资讯订阅:/hltv sub|unsub|status|now|latest|help"""
        args = self._parse_args(event)
        sub = args[0].lower() if args else "help"

        if sub in ("sub", "subscribe", "订阅"):
            umo = event.unified_msg_origin
            subscribers = await self._get_subscribers()
            if umo in subscribers:
                yield event.plain_result("当前会话已经订阅过 HLTV 资讯啦 ✓")
                return
            subscribers.append(umo)
            await self._save_subscribers(subscribers)
            yield event.plain_result(
                "订阅 HLTV 资讯成功 ✓\n"
                "HLTV 全部新闻将自动推送到本会话"
            )

        elif sub in ("unsub", "unsubscribe", "取消订阅"):
            umo = event.unified_msg_origin
            subscribers = await self._get_subscribers()
            if umo not in subscribers:
                yield event.plain_result("当前会话没有订阅 HLTV 资讯")
                return
            subscribers.remove(umo)
            await self._save_subscribers(subscribers)
            yield event.plain_result("已取消订阅 HLTV 资讯")

        elif sub in ("status", "状态"):
            subscribers = await self._get_subscribers()
            seen_count = len(await self.get_kv_data("seen_guids", []) or [])
            enabled = "开启" if bool(self.config.get("enable", True)) else "关闭"
            yield event.plain_result(
                "\n".join(
                    [
                        "📊 HLTV 订阅状态",
                        f"定时轮询:{enabled}(每 {self._interval_minutes()} 分钟)",
                        f"订阅会话数:{len(subscribers)}",
                        f"已记录去重资讯:{seen_count} 条",
                    ]
                )
            )

        elif sub in ("now", "check", "检查"):
            try:
                matched = await self._check_once()
            except Exception as e:
                yield event.plain_result(f"检查失败:{e}{self._fetch_error_hint(e)}")
                return
            if matched:
                subscribers = await self._get_subscribers()
                if subscribers:
                    yield event.plain_result(
                        f"发现 {len(matched)} 条新资讯,已推送到 {len(subscribers)} 个订阅会话"
                    )
                else:
                    yield event.plain_result(
                        f"发现 {len(matched)} 条新资讯,但没有订阅会话,"
                        "发送 /hltv sub 订阅本会话"
                    )
            else:
                yield event.plain_result("暂时没有新的 HLTV 资讯")

        elif sub in ("latest", "最新"):
            try:
                xml_text = await self._fetch_rss()
            except Exception as e:
                yield event.plain_result(
                    f"RSS 拉取失败:{e}{self._fetch_error_hint(e)}"
                )
                return
            items = parse_rss(xml_text)
            if not items:
                yield event.plain_result("当前 RSS 中没有资讯")
                return
            items.sort(
                key=lambda item: item.pub_time
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            item = items[0]
            llm_text = ""
            try:
                llm_text = await self._llm_translate(item)
            except Exception as e:
                logger.warning("[HLTV RSS] LLM 翻译失败(%s):%s", item.title, e)
            image_bytes = await self._prepare_image(item)
            chain, _ = self._build_chains(item, llm_text, image_bytes)
            yield event.chain_result(chain.chain)

        else:
            yield event.plain_result(HELP_TEXT)
