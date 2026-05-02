# astrbot_plugin_dnpush

AstrBot 每日新闻和一言定时推送插件，支持自定义 cron 定时任务、订阅管理。

## 功能

- 每日早报推送（基于 ALAPI）
- 一言推送（基于 hitokoto.cn）
- 自定义 cron 定时表达式
- 群聊/私聊订阅管理
- 支持配置固定推送目标（群号/QQ号）
- 支持 json 文字和 image 图片两种早报格式

## 安装

1. 下载插件 zip 包
2. 在 AstrBot WebUI 插件管理页面上传安装
3. 配置 ALAPI Token（在 [alapi.cn](https://alapi.cn) 注册获取）

## 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `news_api_url` | 新闻 API 地址 | 留空使用 ALAPI 默认接口 |
| `news_api_token` | ALAPI Token（必填） | - |
| `news_format` | 返回格式：`json` 文字 / `image` 图片 | `json` |
| `hitokoto_api_url` | 一言 API 地址 | 留空使用 hitokoto.cn |
| `hitokoto_categories` | 一言分类，逗号分隔 | `a`（全部） |
| `cron_expression` | Cron 定时表达式 | `0 8 * * *`（每天8点） |
| `push_targets` | 推送目标，格式：`group:群号` 或 `private:QQ号` | 空 |
| `push_news` | 是否推送新闻 | `true` |
| `push_hitokoto` | 是否推送一言 | `true` |

## 指令

| 指令 | 说明 |
|------|------|
| `/push news` | 手动推送每日早报 |
| `/push hitokoto` | 手动推送一言 |
| `/push all` | 推送早报 + 一言 |
| `/push subscribe` | 订阅当前会话的定时推送 |
| `/push unsubscribe` | 取消当前会话的定时推送 |
| `/push schedule` | 查看定时任务状态 |
| `/push targets` | 查看所有推送目标 |

## 一言分类

| 参数 | 分类 |
|------|------|
| `a` | 全部 |
| `b` | 动画 |
| `c` | 游戏 |
| `d` | 文学 |
| `e` | 原创 |
| `f` | 网络 |
| `g` | 其他 |
| `h` | 影视 |
| `i` | 诗词 |
| `j` | 哲学 |
| `k` | 科学 |

## Cron 表达式示例

| 表达式 | 说明 |
|--------|------|
| `0 8 * * *` | 每天早上 8 点 |
| `0 8,12,18 * * *` | 每天 8、12、18 点 |
| `30 7 * * 1-5` | 工作日早上 7:30 |
| `0 9 * * 1` | 每周一早上 9 点 |

## 依赖

- `aiohttp`
- `croniter`

## 平台支持

- OneBot v11（aiocqhttp）
