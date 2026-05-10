# Changelog

## v1.4.0

### 代码重构
- 按 AstrBot 插件开发指南规范重构代码
- 删除废弃的 `@register` 装饰器，统一使用 `metadata.yaml` 定义元信息
- 导入路径改为 `astrbot.api.all` 统一导入
- 添加 `event.stop_event()` 阻止事件冒泡
- 合并重复命令 `push_yy` 为 `push_hitokoto` 的 alias
- 移除未使用的 `tempfile` 导入

### API 替换
- 新闻 API 替换为 60 秒图片版（`https://v2.xxapi.cn/api/hot60s`），免费无需 Token
- 一言 API 替换为「在人间凑数的日子」（`https://v2.xxapi.cn/api/renjian`）
- 移除 ALAPI Token 依赖（`news_api_token`、`news_format` 配置项已删除）
- 移除 `hitokoto_categories` 配置项
- 新闻改为图片 URL 直接发送，不再需要下载临时文件

### 文档
- 更新 README.md 同步新 API 和配置说明
- metadata.yaml 添加 `desc` 和 `display_name` 规范字段

## v1.3.0

- 基于 ALAPI 早报和 hitokoto.cn 的定时推送
- 群聊/私聊订阅命令
- HH:MM 每日定时
- 一键开关定时推送
