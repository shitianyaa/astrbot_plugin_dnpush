# astrbot_plugin_dnpush

AstrBot 每日新闻 + 人间语录定时推送插件。每天定点向指定群 / 私聊推送 60 秒新闻图片和「在人间凑数的日子」语录。

## 功能

- 每日 60 秒新闻图片推送（基于 [xxapi.cn](https://v2.xxapi.cn/api/hot60s)）
- 「在人间凑数的日子」语录推送（基于 [xxapi.cn](https://v2.xxapi.cn/api/renjian)）
- HH:MM 每日定时（北京时间，分钟级精度）
- 群聊 / 私聊订阅命令，订阅信息持久化
- 也可在配置里写死推送目标
- 一键开关定时推送，不影响手动推送

## 安装

1. 下载插件 zip 包，AstrBot WebUI → 插件管理 → 上传安装
2. 进入插件配置页按需调整 `push_time`、`push_targets` 等
3. 免费 API 无需额外配置 Token，开箱即用

## 配置项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `push_enabled` | 启用定时推送（关闭后手动指令不受影响） | `true` |
| `push_time` | 每日推送时间，格式 `HH:MM`（北京时间） | `08:00` |
| `news_api_url` | 新闻 API 地址 | 留空使用 `https://v2.xxapi.cn/api/hot60s` |
| `hitokoto_api_url` | 人间语录 API 地址 | 留空使用 `https://v2.xxapi.cn/api/renjian` |
| `push_targets` | 固定推送目标列表 | `[]` |
| `platform_id` | 默认平台适配器 ID（push_targets 中未指定平台的条目使用） | 留空自动检测 |
| `push_news` | 定时推送中是否包含新闻 | `true` |
| `push_hitokoto` | 定时推送中是否包含语录 | `true` |
| `push_target_interval` | 不同目标间发送间隔（秒），防风控 | `1.5` |
| `push_chain_interval` | 同一目标多条消息间隔（秒），防风控 | `1.0` |

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
| `/push news` | 手动推送一次每日 60 秒新闻图片 |
| `/push yy` / `/push hitokoto` | 手动推送一次人间语录 |
| `/push all` | 手动推送新闻 + 人间语录 |
| `/push subscribe` | 在当前会话订阅每日定时推送 |
| `/push unsubscribe` | 取消当前会话的订阅 |
| `/push schedule` | 查看定时任务状态、下次推送时间、平台 ID 等 |
| `/push targets` | 查看所有推送目标（配置目标 + 订阅会话） |

## 时区说明

定时任务使用 `Asia/Shanghai` 时区。在 Linux 上一般无需额外配置；Windows 上若提示找不到时区数据，会自动回退到固定 UTC+8（同等效果），也可以执行 `pip install tzdata` 启用完整 IANA 时区数据库。

## 依赖

- `aiohttp`
- `tzdata`（仅 Windows，自动安装）

## 平台支持

AstrBot 已接入的所有消息平台均可使用。

- **订阅式推送**（`/push subscribe`）：使用会话的 `unified_msg_origin`，所有平台通用，零配置
- **固定目标推送**（`push_targets`）：每条目独立指定平台，可以同时往多个平台推送；未指定时使用 `platform_id` 作为默认平台

## 反馈

Issue / PR 欢迎到 [GitHub 仓库](https://github.com/shitianyaa/astrbot_plugin_dnpush) 提交。
