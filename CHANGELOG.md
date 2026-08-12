# Changelog

所有 noteworthy 的插件更新都会记录在此文件中。

## \[1.3.4] - 2026-08-12

### Added

- 新增 Commit 通知文转图功能，`commit_output_format` 配置项支持 `text` / `image` 两种输出格式
- 新增 6 套内置图片模板主题：terminal（终端风）、github_dark（GitHub 暗色）、light（简洁浅色）、retro_term（复古绿屏）、miku（初音风）、sakura（樱花粉），支持 `random` 随机切换；模板按内容裁剪无空白
- 新增自定义模板支持：在插件数据目录 `templates/` 下放置 `.html` 文件即可使用（Jinja2），并自动加入随机池
- 新增 `enable_base64_image` 配置项，图片传输可选 Base64 编码或本地文件路径；QQ 平台图片通知走 OneBot API 直发
- 新增群文件/群相册备份上传设置（`enable_group_file_upload`、`group_file_folder`、`enable_group_album_upload`、`group_album_name`、`group_album_strict_mode`）
- 新增 T2I 渲染参数配置（`t2i_image_type`、`t2i_quality`、`t2i_scale`），内置两轮渲染回退与图片完整性校验
- 图片通知渲染或发送失败时计入重试队列自动重试

## \[1.3.2] - 2026-07-02

### Added

- 新增 `/github_issues` 指令，查询当前用户所有仓库的 open issues
- 新增 Issues 定时推送功能，支持 Cron 表达式自动推送
- 新增 `issues_cron_enabled` 配置项，控制是否启用 Issues 定时推送
- 新增 `issues_cron_expression` 配置项，设置推送的 Cron 表达式
- 新增 `issues_push_min_interval` 配置项，设置相同内容推送的最小间隔
- 新增 issues 快照对比机制，只推送新增和更新的 issue
- 新增推送间隔保护，防止相同内容短时间内重复推送
- 新增群聊推送支持，Issues 变更通知可同时发送到私聊和群聊
- 优化消息发送逻辑

## \[1.2.5] - 2026-04-16

### Added

- GitHub 仓库 commit 监控功能
- 定时检查仓库更新并发送通知

## \[1.0.0] - 2026-03-20

### Added

- 初始版本发布

