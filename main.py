import asyncio
import datetime
import os
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

try:
    CN_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows 上若未安装 tzdata，回退到固定 UTC+8 偏移，避免插件加载失败
    logger.warning("[Push] 未找到 IANA 时区数据库，回退到固定 UTC+8。建议: pip install tzdata")
    CN_TZ = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")


# ========== 常量 ==========
DEFAULT_PUSH_TIME = "08:00"
DEFAULT_NEWS_URL = "https://v3.alapi.cn/api/zaobao"
DEFAULT_HITOKOTO_URL = "https://v1.hitokoto.cn"
SCHEDULER_POLL_SECONDS = 30  # 调度器轮询间隔
CHAIN_INTERVAL_SECONDS = 1.0  # 同一目标多条消息之间的间隔
TARGET_INTERVAL_SECONDS = 1.5  # 不同目标之间的间隔
KV_KEY_SUBSCRIBERS = "subscribers"
KV_KEY_LAST_PUSH_DATE = "last_push_date"


@register("astrbot_plugin_dnpush", "shitianyaa", "每日新闻和一言定时推送插件", "1.3.0")
class PushPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task: asyncio.Task | None = None
        self._last_push_date: datetime.date | None = None
        self.session: aiohttp.ClientSession | None = None
        # 兜底层 1: 构造器里就尝试起调度器
        self._ensure_scheduler_started(reason="__init__")

    async def initialize(self):
        """插件加载完成回调（每次加载/重载都触发）"""
        self._ensure_session()
        # 恢复上次推送日期，避免重载导致同一天重复推送
        await self._restore_last_push_date()
        # 兜底层 2
        self._ensure_scheduler_started(reason="initialize")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """兜底层 3: AstrBot 主程序加载完成"""
        self._ensure_session()
        self._ensure_scheduler_started(reason="on_loaded")

    async def terminate(self):
        """插件卸载时清理"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        if self.session is not None and not self.session.closed:
            await self.session.close()
        logger.info("[Push] 插件已停止")

    # ========== 调度器 ==========

    def _ensure_scheduler_started(self, reason: str = "") -> None:
        """幂等地启动调度器。已运行则不动；失败时静默（留给后续兜底）"""
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            push_time = self.config.get("push_time", DEFAULT_PUSH_TIME)
            push_enabled = self.config.get("push_enabled", True)
            logger.info(
                f"[Push] 调度器已启动（{reason}），推送时间: {push_time}，启用: {push_enabled}"
            )
        except RuntimeError:
            logger.debug(f"[Push] {reason} 阶段无运行中事件循环，等待下一层兜底")

    def _ensure_session(self) -> aiohttp.ClientSession:
        """确保 session 已创建（懒加载）"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _restore_last_push_date(self) -> None:
        """从 KV 恢复上次推送日期"""
        try:
            last_str = await self.get_kv_data(KV_KEY_LAST_PUSH_DATE, "")
            if last_str:
                self._last_push_date = datetime.date.fromisoformat(last_str)
                logger.info(f"[Push] 已恢复上次推送日期: {self._last_push_date}")
        except Exception as e:
            logger.warning(f"[Push] 恢复 last_push_date 失败（忽略）: {e}")

    async def _save_last_push_date(self, d: datetime.date) -> None:
        """持久化上次推送日期"""
        try:
            await self.put_kv_data(KV_KEY_LAST_PUSH_DATE, d.isoformat())
        except Exception as e:
            logger.warning(f"[Push] 持久化 last_push_date 失败（忽略）: {e}")

    async def _scheduler_loop(self):
        """定时任务循环：30 秒轮询，到点且当天未推送则触发"""
        logger.info("[Push] 调度器循环已启动")
        while True:
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)
            try:
                if not self.config.get("push_enabled", True):
                    continue
                target = self._parse_push_time()
                if target is None:
                    continue
                h, m = target
                now = datetime.datetime.now(CN_TZ)
                target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now < target_time:
                    continue
                if self._last_push_date == now.date():
                    continue
                # 满足条件：先标记后推送，避免推送过程中重入
                self._last_push_date = now.date()
                await self._save_last_push_date(self._last_push_date)
                logger.info(f"[Push] 到达推送时间 {h:02d}:{m:02d}，开始推送")
                await self._do_push()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Push] 调度器异常: {e}", exc_info=True)
                # 异常后等一会儿再继续，避免日志风暴
                await asyncio.sleep(60)

    # ========== 时间 / 配置解析 ==========

    def _parse_push_time(self) -> tuple[int, int] | None:
        """解析配置里的 push_time，返回 (h, m)；失败返回 None 并记日志"""
        raw = self.config.get("push_time", DEFAULT_PUSH_TIME)
        try:
            target_str = str(raw).replace("：", ":").strip()
            h_str, m_str = target_str.split(":")
            h, m = int(h_str), int(m_str)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"超出范围: {h}:{m}")
            return h, m
        except Exception as e:
            logger.error(f"[Push] push_time 配置 {raw!r} 解析失败: {e}")
            return None

    # ========== 平台 / 目标解析 ==========

    def _get_platform(self) -> str:
        """获取推送使用的平台适配器 ID。优先使用用户配置，其次自动检测。"""
        configured = (self.config.get("platform_id", "") or "").strip()
        if configured:
            return configured
        try:
            all_plats = self.context.get_all_platforms()
            if not all_plats:
                return "aiocqhttp"
            if isinstance(all_plats, dict):
                return next(iter(all_plats.keys()))
            first = all_plats[0]
            for attr in ("platform_name", "name"):
                val = getattr(first, attr, None)
                if isinstance(val, str) and val:
                    return val
            meta = getattr(first, "meta", None)
            if callable(meta):
                m = meta()
                name = getattr(m, "name", None)
                if isinstance(name, str) and name:
                    return name
        except Exception as e:
            logger.debug(f"[Push] 平台自动检测失败: {e}")
        return "aiocqhttp"

    def _get_all_targets(self, subscribers: list) -> list[str]:
        """合并配置目标 + 订阅者，解析后去重，保留顺序。

        push_targets 支持的格式：
          - 'group:ID' / 'private:ID'           使用 platform_id 作为平台
          - '平台ID:group:ID' / '平台ID:private:ID'    覆盖默认平台
          - '平台ID:GroupMessage:ID' / '平台ID:FriendMessage:ID'   完整 UMO 原样使用
        """
        # 订阅者类型校验：KV 数据被外部篡改时不至于直接抛
        if not isinstance(subscribers, list):
            logger.warning(f"[Push] subscribers 类型异常 {type(subscribers).__name__}，已重置为空列表")
            subscribers = []

        # 用 dict 保留顺序去重（Python 3.7+ dict 有序）
        seen: dict[str, None] = {}
        for umo in subscribers:
            if isinstance(umo, str) and umo and umo not in seen:
                seen[umo] = None

        config_targets = self.config.get("push_targets", []) or []
        default_plat = self._get_platform()
        logger.debug(
            f"[Push] 解析推送目标: 默认平台={default_plat}, "
            f"配置={config_targets}, 订阅={len(subscribers)}"
        )
        for raw in config_targets:
            if not isinstance(raw, str):
                logger.warning(f"[Push] 跳过非字符串目标: {raw!r}")
                continue
            t = raw.strip().replace("：", ":")
            if not t:
                continue
            umo = self._parse_target_to_umo(t, default_plat)
            if umo is None:
                logger.warning(
                    f"[Push] 跳过格式错误的目标: {raw!r}，支持格式: "
                    "'group:ID' / 'private:ID' / '平台ID:group:ID' / 完整 UMO"
                )
                continue
            if umo not in seen:
                seen[umo] = None

        targets = list(seen.keys())
        logger.debug(f"[Push] 最终推送目标: {targets}")
        return targets

    @staticmethod
    def _parse_target_to_umo(t: str, default_plat: str) -> str | None:
        """把单条 push_targets 解析成 unified_msg_origin，失败返回 None"""
        # 已是完整 UMO（含 GroupMessage/FriendMessage 段）
        if ":GroupMessage:" in t or ":FriendMessage:" in t:
            return t
        parts = t.split(":")
        # 'group:ID' / 'private:ID'
        if len(parts) == 2:
            kind, ident = parts[0].strip().lower(), parts[1].strip()
            if not ident:
                return None
            if kind == "group":
                return f"{default_plat}:GroupMessage:{ident}"
            if kind == "private":
                return f"{default_plat}:FriendMessage:{ident}"
            return None
        # '平台ID:group:ID' / '平台ID:private:ID'
        if len(parts) == 3:
            plat, kind, ident = parts[0].strip(), parts[1].strip().lower(), parts[2].strip()
            if not plat or not ident:
                return None
            if kind == "group":
                return f"{plat}:GroupMessage:{ident}"
            if kind == "private":
                return f"{plat}:FriendMessage:{ident}"
            return None
        # 单段纯数字：兼容只填了 ID 的情况
        if len(parts) == 1 and t.isdigit():
            logger.info(f"[Push] 目标 {t!r} 未带前缀，默认按群处理；建议改为 'group:{t}'")
            return f"{default_plat}:GroupMessage:{t}"
        return None

    @staticmethod
    def _format_umo_human(umo: str) -> str:
        """把 UMO 格式化为人类可读形式"""
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return umo
        plat, kind, ident = parts
        kind_zh = {"GroupMessage": "群", "FriendMessage": "私聊"}.get(kind, kind)
        return f"{kind_zh} {ident} ({plat})"

    # ========== 推送执行 ==========

    async def _do_push(self):
        """执行定时推送到所有目标"""
        subscribers = await self.get_kv_data(KV_KEY_SUBSCRIBERS, [])
        targets = self._get_all_targets(subscribers)
        if not targets:
            logger.info("[Push] 无推送目标，跳过推送")
            return

        push_news = self.config.get("push_news", True)
        push_hitokoto = self.config.get("push_hitokoto", True)
        chains: list[MessageChain] = []
        # 临时图片文件，推送结束后清理
        temp_files: list[str] = []

        if push_news:
            news_text, news_img_path = await self._fetch_news()
            if news_text:
                chains.append(MessageChain([Plain(f"📰 今日早报\n{news_text}")]))
            elif news_img_path:
                chains.append(MessageChain([Image.fromFileSystem(news_img_path)]))
                temp_files.append(news_img_path)
            else:
                chains.append(MessageChain([Plain("📰 新闻获取失败，请检查 ALAPI Token 配置")]))

        if push_hitokoto:
            hitokoto = await self._fetch_hitokoto()
            if hitokoto:
                chains.append(MessageChain([Plain(f"💬 一言\n{hitokoto}")]))

        if not chains:
            self._cleanup_temp_files(temp_files)
            return

        try:
            success = 0
            for i, umo in enumerate(targets):
                try:
                    for j, chain in enumerate(chains):
                        await self.context.send_message(umo, chain)
                        # 同一目标多条消息之间也要 sleep，避免触发风控
                        if j < len(chains) - 1:
                            await asyncio.sleep(CHAIN_INTERVAL_SECONDS)
                    success += 1
                except Exception as e:
                    logger.warning(f"[Push] 推送失败 {umo}: {e}")
                # 不同目标之间 sleep
                if i < len(targets) - 1:
                    await asyncio.sleep(TARGET_INTERVAL_SECONDS)
            logger.info(f"[Push] 定时推送完成，成功 {success}/{len(targets)}")
        finally:
            self._cleanup_temp_files(temp_files)

    @staticmethod
    def _cleanup_temp_files(paths: list[str]) -> None:
        for p in paths:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError as e:
                logger.debug(f"[Push] 清理临时文件失败 {p}: {e}")

    # ========== 数据获取 ==========

    async def _fetch_news(self) -> tuple[str | None, str | None]:
        """获取 ALAPI 每日早报。
        返回 (text, image_path)；image_path 是本地临时文件路径，由调用方负责清理。
        两者至少一个不为 None 表示成功。
        """
        api_url = self.config.get("news_api_url", "") or DEFAULT_NEWS_URL
        token = self.config.get("news_api_token", "")
        fmt = self.config.get("news_format", "json")

        if not token:
            return None, None

        data = {"token": token, "format": fmt}
        try:
            session = self._ensure_session()
            async with session.post(
                api_url, data=data, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[Push] ALAPI 返回 {resp.status}")
                    return None, None

                if fmt == "image":
                    # ALAPI image 模式直接返回图片二进制流
                    img_bytes = await resp.read()
                    if not img_bytes:
                        logger.error("[Push] ALAPI 图片响应为空")
                        return None, None
                    # 写入临时文件，由调用方清理
                    fd, path = tempfile.mkstemp(prefix="dnpush_news_", suffix=".jpg")
                    try:
                        with os.fdopen(fd, "wb") as f:
                            f.write(img_bytes)
                    except Exception:
                        if os.path.exists(path):
                            os.remove(path)
                        raise
                    return None, path

                result = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[Push] ALAPI 请求失败: {e}")
            return None, None

        if result.get("code") != 200:
            logger.error(f"[Push] ALAPI 错误: {result.get('msg', '未知错误')}")
            return None, None

        api_data = result.get("data", {})
        date = api_data.get("date", "")
        news = api_data.get("news", "")
        weiyu = api_data.get("weiyu", "")

        parts = []
        if date:
            parts.append(f"📅 {date}")
        if isinstance(news, list):
            parts.extend(str(item) for item in news)
        elif news:
            parts.append(news)
        if weiyu:
            parts.append(f"\n💡 {weiyu}")

        text = "\n".join(parts) if parts else None
        return text, None

    async def _fetch_hitokoto(self) -> str | None:
        """获取一言"""
        api_url = self.config.get("hitokoto_api_url", "") or DEFAULT_HITOKOTO_URL
        categories = self.config.get("hitokoto_categories", "a")

        # 去重，保留首次出现顺序
        cats_seen: dict[str, None] = {}
        for cat in str(categories).split(","):
            cat = cat.strip()
            if cat and cat not in cats_seen:
                cats_seen[cat] = None
        params = {"c": list(cats_seen.keys())} if cats_seen else {}

        try:
            session = self._ensure_session()
            async with session.get(
                api_url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[Push] 一言 API 请求失败: {e}")
            return None

        hitokoto = data.get("hitokoto", "")
        source = data.get("from", "")
        author = data.get("from_who", "")

        if not hitokoto:
            return None

        result = hitokoto
        if source or author:
            result += "\n"
            if author and source:
                result += f"——{author}《{source}》"
            elif author:
                result += f"——{author}"
            elif source:
                result += f"——《{source}》"

        return result

    # ========== 指令组 ==========

    @filter.command_group("push")
    def push(self):
        """新闻和一言推送指令组"""
        pass

    @push.command("news")
    async def push_news(self, event: AstrMessageEvent):
        """手动推送每日早报"""
        news_text, news_img_path = await self._fetch_news()
        try:
            if news_text:
                yield event.plain_result(f"📰 今日早报\n{news_text}")
            elif news_img_path:
                yield event.image_result(news_img_path)
            else:
                yield event.plain_result("新闻获取失败，请检查 ALAPI Token 配置")
        finally:
            if news_img_path:
                self._cleanup_temp_files([news_img_path])

    @push.command("hitokoto")
    async def push_hitokoto(self, event: AstrMessageEvent):
        """手动推送一言"""
        hitokoto = await self._fetch_hitokoto()
        if hitokoto:
            yield event.plain_result(f"💬 一言\n{hitokoto}")
        else:
            yield event.plain_result("一言获取失败，请检查 API 配置")

    @push.command("all")
    async def push_all(self, event: AstrMessageEvent):
        """推送早报 + 一言"""
        news_text, news_img_path = await self._fetch_news()
        try:
            if news_text:
                yield event.plain_result(f"📰 今日早报\n{news_text}")
            elif news_img_path:
                yield event.image_result(news_img_path)
            else:
                yield event.plain_result("📰 新闻获取失败")
        finally:
            if news_img_path:
                self._cleanup_temp_files([news_img_path])

        hitokoto = await self._fetch_hitokoto()
        if hitokoto:
            yield event.plain_result(f"💬 一言\n{hitokoto}")

    @push.command("subscribe")
    async def push_subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的定时推送"""
        umo = event.unified_msg_origin
        subscribers = await self.get_kv_data(KV_KEY_SUBSCRIBERS, [])
        if not isinstance(subscribers, list):
            subscribers = []
        if umo in subscribers:
            yield event.plain_result("当前会话已订阅，无需重复操作")
            return
        subscribers.append(umo)
        await self.put_kv_data(KV_KEY_SUBSCRIBERS, subscribers)
        push_time = self.config.get("push_time", DEFAULT_PUSH_TIME)
        yield event.plain_result(f"订阅成功！每日推送时间: {push_time}")

    @push.command("unsubscribe")
    async def push_unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的定时推送"""
        umo = event.unified_msg_origin
        subscribers = await self.get_kv_data(KV_KEY_SUBSCRIBERS, [])
        if not isinstance(subscribers, list) or umo not in subscribers:
            yield event.plain_result("当前会话未订阅")
            return
        subscribers.remove(umo)
        await self.put_kv_data(KV_KEY_SUBSCRIBERS, subscribers)
        yield event.plain_result("已取消订阅")

    @push.command("schedule")
    async def push_schedule(self, event: AstrMessageEvent):
        """查看定时任务状态"""
        push_time = self.config.get("push_time", DEFAULT_PUSH_TIME)
        push_enabled = self.config.get("push_enabled", True)
        subscribers = await self.get_kv_data(KV_KEY_SUBSCRIBERS, [])
        if not isinstance(subscribers, list):
            subscribers = []
        config_targets = self.config.get("push_targets", []) or []
        push_news_on = self.config.get("push_news", True)
        push_hitokoto_on = self.config.get("push_hitokoto", True)
        configured_url = self.config.get("news_api_url", "")
        news_url_display = configured_url if configured_url else f"{DEFAULT_NEWS_URL}（默认）"

        all_targets = self._get_all_targets(subscribers)

        target = self._parse_push_time()
        if target is None:
            next_str = "时间格式错误"
        else:
            h, m = target
            now = datetime.datetime.now(CN_TZ)
            target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now < target_time:
                next_str = target_time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                next_str = (target_time + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

        parts = [
            "⏰ 定时推送状态",
            f"推送时间: 每天 {push_time}",
            f"推送状态: {'✅ 已启用' if push_enabled else '❌ 已禁用'}",
            f"下次推送: {next_str}",
            f"上次推送: {self._last_push_date or '尚未推送'}",
            f"推送新闻: {'✅' if push_news_on else '❌'}",
            f"推送一言: {'✅' if push_hitokoto_on else '❌'}",
            f"新闻 API: {news_url_display}",
            f"默认平台 ID: {self._get_platform()}",
            f"配置目标: {len(config_targets)} 个",
            f"订阅会话: {len(subscribers)} 个",
            f"总推送目标: {len(all_targets)} 个",
        ]
        yield event.plain_result("\n".join(parts))

    @push.command("targets")
    async def push_targets(self, event: AstrMessageEvent):
        """查看所有推送目标"""
        subscribers = await self.get_kv_data(KV_KEY_SUBSCRIBERS, [])
        if not isinstance(subscribers, list):
            subscribers = []
        config_targets = self.config.get("push_targets", []) or []
        all_targets = self._get_all_targets(subscribers)
        default_plat = self._get_platform()

        parts = ["📋 推送目标列表"]

        if config_targets:
            parts.append("\n[配置目标]")
            for raw in config_targets:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                t = raw.strip().replace("：", ":")
                umo = self._parse_target_to_umo(t, default_plat)
                if umo is None:
                    parts.append(f"  ⚠️ 格式错误: {raw}")
                else:
                    parts.append(f"  {self._format_umo_human(umo)}")

        if subscribers:
            parts.append("\n[订阅会话]")
            for umo in subscribers:
                if isinstance(umo, str):
                    parts.append(f"  {self._format_umo_human(umo)}")

        if not config_targets and not subscribers:
            parts.append("暂无推送目标")

        parts.append(f"\n共 {len(all_targets)} 个目标（已去重）")
        yield event.plain_result("\n".join(parts))
