# Changelog

所有 noteworthy 的插件更新都会记录在此文件中。

## \[1.4.0] - 2026-08-26

### Added

- 新增项目仓库 Issues 动态监控模块：随每次仓库检查一同检测所配置监控仓库的 Issues，有新增 Issue、或 Issue 下出现新讨论（新增评论/标题正文标签更新）时立即推送通知，新评论附带评论者与内容摘要
- 新增 `issue_monitor_enabled` 配置项（默认开启），控制是否启用 Issues 动态监控；推送目标与 Commit 通知一致（私聊 + 全局群 + 仓库专属群）
- 首次纳入监控的仓库仅建立基线快照不推送，避免开启功能时被存量 Issues 刷屏

## \[1.3.6] - 2026-08-25

### Fixed

- 修复多平台同时运行时，直接填写官方 bot 的 openid（非数字会话ID）作为推送目标会被固定优先级自动检测误路由到 aiocqhttp 导致发送失败的问题：自动检测现按目标 ID 特征智能选平台——纯数字 ID → aiocqhttp 优先；非数字 ID（官方bot的十六进制 openid）→ qq_official / qq_official_webhook 优先，无需手动关闭其他适配器
- 修复 UMO 消息类型仅兼容枚举值（`GroupMessage`）的问题：现同时兼容枚举名（`GROUP_MESSAGE`），避免复制到枚举名时解析失败而静默回退自动检测

## \[1.3.5] - 2026-08-24

### Fixed

- 修复 QQ 官方机器人（qq_official / qq_official_webhook）无法作为主动推送目标的问题：移除推送链路中对群号/QQ号的纯数字硬校验（`int()` / `isdigit()`），十六进制 openid 等非数字会话ID不再被误判为非法
- 新增 UMO（unified_msg_origin）格式推送目标支持：`平台ID:消息类型:会话ID`，如 `aiocqhttp:GroupMessage:123456`、`小爱同学:GroupMessage:0771687B325FC423AD9F4C06A88D84E3`，适配多 bot / 多平台场景，可明确指定由哪个平台的哪个会话接收推送
- 向后兼容：纯数字群号/QQ号及 "-" 开头的 Telegram 群ID继续按原有逻辑处理；Issues 定时推送同步适配
- 图片通知的 OneBot 直发通道兼容 UMO 目标（平台为 aiocqhttp 且会话为数字ID时仍走直发）

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

