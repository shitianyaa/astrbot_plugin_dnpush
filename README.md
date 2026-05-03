# astrbot_plugin_dnpush

AstrBot 每日新闻 + 一言定时推送插件。每天定点向指定群 / 私聊推送 ALAPI 早报和一言。

## 功能

- 每日早报推送（基于 [ALAPI](https://alapi.cn)，支持文字 / 图片两种格式）
- 一言推送（基于 [hitokoto.cn](https://hitokoto.cn)，支持分类筛选）
- HH:MM 每日定时（北京时间，分钟级精度）
- 群聊 / 私聊订阅命令，订阅信息持久化
- 也可在配置里写死推送目标
- 一键开关定时推送，不影响手动推送

## 安装

1. 下载插件 zip 包，AstrBot WebUI → 插件管理 → 上传安装
2. 在 [alapi.cn](https://alapi.cn) 注册获取 Token
3. 进入插件配置页填入 `news_api_token`，按需调整 `push_time`、`push_targets` 等

## 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `push_enabled` | 启用定时推送（关闭后手动指令不受影响） | `true` |
| `push_time` | 每日推送时间，格式 `HH:MM`（北京时间） | `08:00` |
| `news_api_url` | 新闻 API 地址 | 留空使用 `https://v3.alapi.cn/api/zaobao` |
| `news_api_token` | ALAPI Token（必填） | - |
| `news_format` | 返回格式：`json` 文字 / `image` 图片 | `json` |
| `hitokoto_api_url` | 一言 API 地址 | 留空使用 `https://v1.hitokoto.cn` |
| `hitokoto_categories` | 一言分类，多个用逗号分隔 | `a` |
| `push_targets` | 固定推送目标列表 | `[]` |
| `platform_id` | 默认平台适配器 ID（push_targets 中未指定平台的条目使用） | 留空自动检测 |
| `push_news` | 定时推送中是否包含新闻 | `true` |
| `push_hitokoto` | 定时推送中是否包含一言 | `true` |

### push_targets 格式

每行一个，支持三种写法并存：

```
group:123456789                  # 使用 platform_id 作为平台
private:10086                    # 同上
napcat:group:123456789           # 单独指定平台（覆盖 platform_id）
telegram:private:987654321       # 同上
aiocqhttp:GroupMessage:123       # 完整的 unified_msg_origin，原样使用
```

兼容中文冒号 `：` 和大小写 `Group/Private`。各平台 ID 含义不同（QQ 群号、Telegram chat_id、Discord channel_id 等），按你的适配器实际接受的格式填写。

> 想同时往多个平台推送时，给每条目加上对应的「平台ID:」前缀即可。

### platform_id 说明

`push_targets` 中未指定平台的条目（如 `group:123`）会使用此值作为平台 ID。AstrBot 允许给同一类型的适配器起自定义 ID，如果定时推送日志里出现 `cannot find platform for session ...`，说明自动检测到的 ID 跟你实际启用的适配器对不上，请到 AstrBot 「消息平台」页面查看你启用的适配器 ID，手动填到 `platform_id` 里。

## 指令

所有指令都在 `/push` 指令组下：

| 指令 | 说明 |
|------|------|
| `/push news` | 手动推送一次每日早报 |
| `/push hitokoto` | 手动推送一次一言 |
| `/push all` | 手动推送早报 + 一言 |
| `/push subscribe` | 在当前会话订阅每日定时推送 |
| `/push unsubscribe` | 取消当前会话的订阅 |
| `/push schedule` | 查看定时任务状态、下次推送时间、平台 ID 等 |
| `/push targets` | 查看所有推送目标（配置目标 + 订阅会话） |

## 一言分类

| 参数 | 分类 | 参数 | 分类 |
|------|------|------|------|
| `a` | 全部 | `g` | 其他 |
| `b` | 动画 | `h` | 影视 |
| `c` | 游戏 | `i` | 诗词 |
| `d` | 文学 | `j` | 哲学 |
| `e` | 原创 | `k` | 科学 |
| `f` | 网络 | | |

多分类用逗号分隔，如 `a,d,i`。

## 时区说明

定时任务使用 `Asia/Shanghai` 时区。在 Linux 上一般无需额外配置；Windows 上若提示找不到时区数据，会自动回退到固定 UTC+8（同等效果），也可以执行 `pip install tzdata` 启用完整 IANA 时区数据库。

## 依赖

- `aiohttp`
- `tzdata`（仅 Windows，自动安装）

## 平台支持

`all` —— AstrBot 已接入的所有消息平台均可使用。

- **订阅式推送**（`/push subscribe`）：使用会话的 `unified_msg_origin`，所有平台通用，零配置
- **固定目标推送**（`push_targets`）：每条目独立指定平台，可以同时往多个平台推送；未指定时使用 `platform_id` 作为默认平台

## 反馈

Issue / PR 欢迎到 [GitHub 仓库](https://github.com/shitianyaa/astrbot_plugin_dnpush) 提交。
