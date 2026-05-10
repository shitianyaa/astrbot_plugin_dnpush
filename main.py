import asyncio
import datetime
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from astrbot.api.all import AstrBotConfig, Context, Image, MessageChain, Plain, Star, logger
from astrbot.api.event import AstrMessageEvent, filter

try:
    CN_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows 上若未安装 tzdata，回退到固定 UTC+8 偏移，避免插件加载失败
    logger.warning("[Push] 未找到 IANA 时区数据库，回退到固定 UTC+8。建议: pip install tzdata")
    CN_TZ = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Shanghai")


# ========== 常量 ==========
DEFAULT_PUSH_TIME = "08:00"
DEFAULT_NEWS_URL = "https://v2.xxapi.cn/api/hot60s"
DEFAULT_HITOKOTO_URL = "https://v2.xxapi.cn/api/renjian"
SCHEDULER_POLL_SECONDS = 30  # 平时轮询间隔
SCHEDULER_COOLDOWN_SECONDS = 60  # 推送后睡过整个目标分钟，避免同分钟内重复触发
DEFAULT_CHAIN_INTERVAL = 1.0  # 同一目标多条消息之间的默认间隔
DEFAULT_TARGET_INTERVAL = 1.5  # 不同目标之间的默认间隔
CHAIN_INTERVAL_RANGE = (0.0, 30.0)  # 同目标间隔取值范围
TARGET_INTERVAL_RANGE = (0.5, 60.0)  # 目标间间隔取值范围
KV_KEY_SUBSCRIBERS = "subscribers"


class PushPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task: asyncio.Task | None = None
        self.session: aiohttp.ClientSession | None = None
        # 兜底层 1: 构造器里就尝试起调度器
        self._ensure_scheduler_started(reason="__init__")

    async def initialize(self):
        """插件加载完成回调（每次加载/重载都触发）"""
        self._ensure_session()
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

    async def _scheduler_loop(self):
        """定时任务循环：30 秒轮询，当前分钟匹配目标分钟即触发。

        匹配后睡满 60 秒（SCHEDULER_COOLDOWN_SECONDS）过完目标分钟，
        避免同分钟内的下次轮询重复触发。无任何"已推送"状态。
        """
        logger.info("[Push] 调度器循环已启动")
        while True:
            try:
                triggered = False
                if self.config.get("push_enabled", True):
                    target = self._parse_push_time()
                    if target is not None:
                        h, m = target
                        now = datetime.datetime.now(CN_TZ)
                        if now.hour == h and now.minute == m:
                            logger.info(
                                f"[Push] 到达推送时间 {h:02d}:{m:02d}，开始推送"
                            )
                            await self._do_push()
                            triggered = True
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Push] 调度器异常: {e}", exc_info=True)
                await asyncio.sleep(60)
                continue
            # 触发后睡过整个目标分钟，否则正常 30s 轮询
            await asyncio.sleep(
                SCHEDULER_COOLDOWN_SECONDS if triggered else SCHEDULER_POLL_SECONDS
            )

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

    def _get_interval(
        self, key: str, default: float, value_range: tuple[float, float]
    ) -> float:
        """读取并校验间隔类配置，超出范围或非法时使用默认值并告警"""
        raw = self.config.get(key, default)
        lo, hi = value_range
        try:
            val = float(raw)
        except (TypeError, ValueError):
            logger.warning(f"[Push] {key} 配置 {raw!r} 不是数字，使用默认 {default}s")
            return default
        if val < lo or val > hi:
            logger.warning(
                f"[Push] {key}={val} 超出允许范围 [{lo}, {hi}]，使用默认 {default}s"
            )
            return default
        return val

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
            news_text, news_img_url = await self._fetch_news()
            if news_text:
                chains.append(MessageChain([Plain(f"📰 今日早报\n{news_text}")]))
            elif news_img_url:
                chains.append(MessageChain([Image.fromURL(news_img_url)]))
            else:
                chains.append(MessageChain([Plain("📰 新闻获取失败，请检查 API 配置")]))

        if push_hitokoto:
            hitokoto = await self._fetch_hitokoto()
            if hitokoto:
                chains.append(MessageChain([Plain(f"💬 人间语录\n{hitokoto}")]))

        if not chains:
            self._cleanup_temp_files(temp_files)
            return

        try:
            chain_interval = self._get_interval(
                "push_chain_interval", DEFAULT_CHAIN_INTERVAL, CHAIN_INTERVAL_RANGE
            )
            target_interval = self._get_interval(
                "push_target_interval", DEFAULT_TARGET_INTERVAL, TARGET_INTERVAL_RANGE
            )
            success = 0
            for i, umo in enumerate(targets):
                try:
                    for j, chain in enumerate(chains):
                        await self.context.send_message(umo, chain)
                        # 同一目标多条消息之间也要 sleep，避免触发风控
                        if j < len(chains) - 1 and chain_interval > 0:
                            await asyncio.sleep(chain_interval)
                    success += 1
                except Exception as e:
                    logger.warning(f"[Push] 推送失败 {umo}: {e}")
                # 不同目标之间 sleep
                if i < len(targets) - 1 and target_interval > 0:
                    await asyncio.sleep(target_interval)
            logger.info(
                f"[Push] 定时推送完成，成功 {success}/{len(targets)}"
                f"（目标间 {target_interval}s，同目标内 {chain_interval}s）"
            )
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
        """获取每日 60 秒新闻图片。
        返回 (text, image_url)；image_url 是可直接发送的图片 URL。
        两者至少一个不为 None 表示成功。
        """
        api_url = self.config.get("news_api_url", "") or DEFAULT_NEWS_URL

        try:
            session = self._ensure_session()
            async with session.get(
                api_url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[Push] 60s API 返回 {resp.status}")
                    return None, None
                result = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[Push] 60s API 请求失败: {e}")
            return None, None

        if result.get("code") != 200:
            logger.error(f"[Push] 60s API 错误: {result.get('msg', '未知错误')}")
            return None, None

        data = result.get("data", "")
        # data 可能是 str 或 list，做类型安全处理
        if isinstance(data, list):
            if not data:
                logger.error("[Push] 60s API 返回空数据列表")
                return None, None
            image_url = str(data[0])
        elif isinstance(data, str):
            image_url = data
        else:
            logger.error(f"[Push] 60s API data 类型异常: {type(data).__name__}")
            return None, None

        if not image_url:
            logger.error("[Push] 60s API 未返回图片 URL")
            return None, None

        return None, image_url

    async def _fetch_hitokoto(self) -> str | None:
        """获取「在人间凑数的日子」语录"""
        api_url = self.config.get("hitokoto_api_url", "") or DEFAULT_HITOKOTO_URL

        try:
            session = self._ensure_session()
            async with session.get(
                api_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[Push] 人间语录 API 请求失败: {e}")
            return None

        if data.get("code") != 200:
            return None

        quote = data.get("data", "")
        return quote if quote else None

    # ========== 指令组 ==========

    @filter.command_group("push")
    def push(self):
        """新闻和一言推送指令组"""
        pass

    @push.command("news")
    async def push_news(self, event: AstrMessageEvent):
        """手动推送每日 60 秒新闻"""
        event.stop_event()
        news_text, news_img_url = await self._fetch_news()
        if news_text:
            yield event.plain_result(f"📰 今日早报\n{news_text}")
        elif news_img_url:
            yield event.chain_result([Image.fromURL(news_img_url)])
        else:
            yield event.plain_result("📰 新闻获取失败，请检查 API 配置")

    async def _build_hitokoto_result(self, event: AstrMessageEvent):
        hitokoto = await self._fetch_hitokoto()
        if hitokoto:
            return event.plain_result(f"💬 人间语录\n{hitokoto}")
        return event.plain_result("人间语录获取失败，请检查 API 配置")

    @push.command("hitokoto", alias={"yy"})
    async def push_hitokoto(self, event: AstrMessageEvent):
        """手动推送人间语录"""
        event.stop_event()
        yield await self._build_hitokoto_result(event)

    @push.command("all")
    async def push_all(self, event: AstrMessageEvent):
        """推送早报 + 一言"""
        event.stop_event()
        news_text, news_img_url = await self._fetch_news()
        if news_text:
            yield event.plain_result(f"📰 今日早报\n{news_text}")
        elif news_img_url:
            yield event.chain_result([Image.fromURL(news_img_url)])
        else:
            yield event.plain_result("📰 新闻获取失败")

        hitokoto = await self._fetch_hitokoto()
        if hitokoto:
            yield event.plain_result(f"💬 人间语录\n{hitokoto}")

    @push.command("subscribe")
    async def push_subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的定时推送"""
        event.stop_event()
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
        event.stop_event()
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
        event.stop_event()
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
        event.stop_event()
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
