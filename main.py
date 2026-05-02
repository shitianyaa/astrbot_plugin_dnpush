import asyncio
from datetime import datetime, timezone

import aiohttp
from croniter import croniter

from astrbot.api.event import MessageChain, filter, AstrMessageEvent
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig


@register("astrbot_plugin_dnpush", "shitianyaa", "每日新闻和一言定时推送插件", "1.2.2")
class PushPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._scheduler_task: asyncio.Task | None = None

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """AstrBot 初始化完成后启动定时任务"""
        self._start_scheduler()

    def _start_scheduler(self):
        """启动定时推送调度器"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"[Push] 定时任务已启动，cron: {self.config.get('cron_expression', '0 8 * * *')}")

    async def _scheduler_loop(self):
        """定时任务循环"""
        cron_expr = self.config.get("cron_expression", "0 8 * * *")
        while True:
            try:
                now = datetime.now(timezone.utc)
                cron = croniter(cron_expr, now)
                next_time = cron.get_next(datetime)
                wait_seconds = (next_time - now).total_seconds()
                logger.info(f"[Push] 下次推送时间: {next_time.isoformat()}, 等待 {wait_seconds:.0f} 秒")
                await asyncio.sleep(wait_seconds)
                await self._do_push()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Push] 定时任务异常: {e}")
                await asyncio.sleep(60)

    def _get_all_targets(self, subscribers: list[str]) -> list[str]:
        """获取所有推送目标：配置目标 + 订阅者，去重"""
        targets = list(subscribers)
        config_targets = self.config.get("push_targets", [])
        for t in config_targets:
            t = t.strip()
            if not t:
                continue
            if t.startswith("group:"):
                umo = f"aiocqhttp:group:{t[6:]}"
            elif t.startswith("private:"):
                umo = f"aiocqhttp:private:{t[8:]}"
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
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        logger.error(f"[Push] ALAPI 返回 {resp.status}")
                        return None, None

                    if fmt == "image":
                        # image 格式会重定向到图片地址
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
        if news:
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
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
        cron_expr = self.config.get("cron_expression", "0 8 * * *")
        yield event.plain_result(f"订阅成功！定时推送时间: {cron_expr}")

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
        cron_expr = self.config.get("cron_expression", "0 8 * * *")
        subscribers = await self.get_kv_data("subscribers", [])
        config_targets = self.config.get("push_targets", [])
        push_news = self.config.get("push_news", True)
        push_hitokoto = self.config.get("push_hitokoto", True)
        news_url = self.config.get("news_api_url", "未配置")

        now = datetime.now(timezone.utc)
        next_time = croniter(cron_expr, now).get_next(datetime)

        all_targets = self._get_all_targets(subscribers)

        parts = [
            "⏰ 定时推送状态",
            f"Cron: {cron_expr}",
            f"下次推送: {next_time.strftime('%Y-%m-%d %H:%M:%S')} UTC",
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
        logger.info("[Push] 插件已停止")
