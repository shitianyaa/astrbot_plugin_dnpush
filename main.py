import asyncio
import datetime
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")

import aiohttp

from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig


@register("astrbot_plugin_dnpush", "shitianyaa", "每日新闻和一言定时推送插件", "1.3.0")
class PushPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task: asyncio.Task | None = None
        self._last_push_date = None
        self.session = aiohttp.ClientSession()

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 初始化完成后启动定时任务"""
        self._start_scheduler()

    def _start_scheduler(self):
        """启动定时推送调度器"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        push_time = self.config.get("push_time", "08:00")
        push_enabled = self.config.get("push_enabled", True)
        logger.info(f"[Push] 定时任务已启动，推送时间: {push_time}，启用: {push_enabled}，任务状态: {self._scheduler_task}")

    async def _scheduler_loop(self):
        """定时任务循环，每 30 秒检查一次是否到达推送时间"""
        logger.info("[Push] 调度器循环已启动")
        while True:
            await asyncio.sleep(30)
            try:
                push_enabled = self.config.get("push_enabled", True)
                if not push_enabled:
                    logger.debug("[Push] 定时推送已禁用，跳过")
                    continue
                now = datetime.datetime.now(CN_TZ)
                target_str = self.config.get("push_time", "08:00").replace("：", ":").strip()
                h, m = map(int, target_str.split(":"))
                target_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
                reached = now >= target_time
                already_pushed = self._last_push_date == now.date()
                logger.debug(
                    f"[Push] 检查: 当前={now.strftime('%H:%M:%S')}, "
                    f"目标={target_str}, 已到={reached}, 已推送={already_pushed}, "
                    f"last_push_date={self._last_push_date}"
                )
                if reached and not already_pushed:
                    self._last_push_date = now.date()
                    logger.info(f"[Push] 到达推送时间 {target_str}，开始推送")
                    await self._do_push()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Push] 调度器异常: {e}", exc_info=True)

    def _get_platform(self) -> str:
        """自动检测当前平台"""
        try:
            all_plats = self.context.get_all_platforms()
            if all_plats:
                return list(all_plats.keys())[0]
        except Exception:
            pass
        return "aiocqhttp"

    def _get_all_targets(self, subscribers: list[str]) -> list[str]:
        """获取所有推送目标：配置目标 + 订阅者，去重"""
        targets = list(subscribers)
        config_targets = self.config.get("push_targets", [])
        plat = self._get_platform()
        for t in config_targets:
            t = t.strip()
            if not t:
                continue
            if t.startswith("group:"):
                umo = f"{plat}:GroupMessage:{t[6:]}"
            elif t.startswith("private:"):
                umo = f"{plat}:FriendMessage:{t[8:]}"
            else:
                continue
            if umo not in targets:
                targets.append(umo)
        return targets

    async def _do_push(self):
        """执行定时推送到所有目标"""
        subscribers = await self.get_kv_data("subscribers", [])
        targets = self._get_all_targets(subscribers)
        if not targets:
            logger.info("[Push] 无推送目标，跳过推送")
            return

        push_news = self.config.get("push_news", True)
        push_hitokoto = self.config.get("push_hitokoto", True)
        chains = []

        if push_news:
            news_text, news_img = await self._fetch_news()
            if news_text:
                chains.append(MessageChain([Plain(f"📰 今日早报\n{news_text}")]))
            elif news_img:
                chains.append(MessageChain([Image.fromURL(news_img)]))
            else:
                chains.append(MessageChain([Plain("📰 新闻获取失败，请检查 ALAPI Token 配置")]))

        if push_hitokoto:
            hitokoto = await self._fetch_hitokoto()
            if hitokoto:
                chains.append(MessageChain([Plain(f"💬 一言\n{hitokoto}")]))

        if not chains:
            return

        success = 0
        for umo in targets:
            try:
                for chain in chains:
                    await self.context.send_message(umo, chain)
                success += 1
            except Exception as e:
                logger.warning(f"[Push] 推送失败 {umo}: {e}")
            await asyncio.sleep(1.5)

        logger.info(f"[Push] 定时推送完成，成功 {success}/{len(targets)}")

    # ========== 数据获取 ==========

    async def _fetch_news(self) -> tuple[str | None, str | None]:
        """获取 ALAPI 每日早报。返回 (text, image_url)，至少一个不为 None"""
        api_url = self.config.get("news_api_url", "") or "https://v3.alapi.cn/api/zaobao"
        token = self.config.get("news_api_token", "")
        fmt = self.config.get("news_format", "json")

        if not token:
            return None, None

        data = {"token": token, "format": fmt}

        try:
            async with self.session.post(api_url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.error(f"[Push] ALAPI 返回 {resp.status}")
                    return None, None

                if fmt == "image":
                    return None, str(resp.url)

                result = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[Push] ALAPI 请求失败: {e}")
            return None, None

        if result.get("code") != 200:
            logger.error(f"[Push] ALAPI 错误: {result.get('msg', '未知错误')}")
            return None, None

        data = result.get("data", {})
        date = data.get("date", "")
        news = data.get("news", "")
        weiyu = data.get("weiyu", "")

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
        api_url = self.config.get("hitokoto_api_url", "") or "https://v1.hitokoto.cn"
        categories = self.config.get("hitokoto_categories", "a")

        params = {}
        for cat in categories.split(","):
            cat = cat.strip()
            if cat:
                params.setdefault("c", []).append(cat)

        try:
            async with self.session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
        news_text, news_img = await self._fetch_news()
        if news_text:
            yield event.plain_result(f"📰 今日早报\n{news_text}")
        elif news_img:
            yield event.image_result(news_img)
        else:
            yield event.plain_result("新闻获取失败，请检查 ALAPI Token 配置")

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
        news_text, news_img = await self._fetch_news()
        if news_text:
            yield event.plain_result(f"📰 今日早报\n{news_text}")
        elif news_img:
            yield event.image_result(news_img)
        else:
            yield event.plain_result("📰 新闻获取失败")

        hitokoto = await self._fetch_hitokoto()
        if hitokoto:
            yield event.plain_result(f"💬 一言\n{hitokoto}")

    @push.command("subscribe")
    async def push_subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的定时推送"""
        umo = event.unified_msg_origin
        subscribers = await self.get_kv_data("subscribers", [])
        if umo in subscribers:
            yield event.plain_result("当前会话已订阅，无需重复操作")
            return
        subscribers.append(umo)
        await self.put_kv_data("subscribers", subscribers)
        push_time = self.config.get("push_time", "08:00")
        yield event.plain_result(f"订阅成功！每日推送时间: {push_time}")

    @push.command("unsubscribe")
    async def push_unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的定时推送"""
        umo = event.unified_msg_origin
        subscribers = await self.get_kv_data("subscribers", [])
        if umo not in subscribers:
            yield event.plain_result("当前会话未订阅")
            return
        subscribers.remove(umo)
        await self.put_kv_data("subscribers", subscribers)
        yield event.plain_result("已取消订阅")

    @push.command("schedule")
    async def push_schedule(self, event: AstrMessageEvent):
        """查看定时任务状态"""
        push_time = self.config.get("push_time", "08:00")
        push_enabled = self.config.get("push_enabled", True)
        subscribers = await self.get_kv_data("subscribers", [])
        config_targets = self.config.get("push_targets", [])
        push_news = self.config.get("push_news", True)
        push_hitokoto = self.config.get("push_hitokoto", True)
        news_url = self.config.get("news_api_url", "未配置")

        all_targets = self._get_all_targets(subscribers)

        now = datetime.datetime.now(CN_TZ)
        try:
            h, m = map(int, push_time.replace("：", ":").strip().split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now < target:
                next_str = target.strftime("%Y-%m-%d %H:%M:%S")
            else:
                next_str = (target + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            next_str = "时间格式错误"

        parts = [
            "⏰ 定时推送状态",
            f"推送时间: 每天 {push_time}",
            f"推送状态: {'✅ 已启用' if push_enabled else '❌ 已禁用'}",
            f"下次推送: {next_str}",
            f"推送新闻: {'✅' if push_news else '❌'}",
            f"推送一言: {'✅' if push_hitokoto else '❌'}",
            f"新闻 API: {news_url}",
            f"配置目标: {len(config_targets)} 个",
            f"订阅会话: {len(subscribers)} 个",
            f"总推送目标: {len(all_targets)} 个",
        ]
        yield event.plain_result("\n".join(parts))

    @push.command("targets")
    async def push_targets(self, event: AstrMessageEvent):
        """查看所有推送目标"""
        subscribers = await self.get_kv_data("subscribers", [])
        config_targets = self.config.get("push_targets", [])
        all_targets = self._get_all_targets(subscribers)

        parts = ["📋 推送目标列表"]

        if config_targets:
            parts.append("\n[配置目标]")
            for t in config_targets:
                t = t.strip()
                if t.startswith("group:"):
                    parts.append(f"  群 {t[6:]}")
                elif t.startswith("private:"):
                    parts.append(f"  私聊 {t[8:]}")

        if subscribers:
            parts.append("\n[订阅会话]")
            for umo in subscribers:
                parts.append(f"  {umo}")

        if not config_targets and not subscribers:
            parts.append("暂无推送目标")

        parts.append(f"\n共 {len(all_targets)} 个目标")
        yield event.plain_result("\n".join(parts))

    async def terminate(self):
        """插件卸载时清理"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("[Push] 插件已停止")
