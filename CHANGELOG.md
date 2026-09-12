# Changelog

所有 noteworthy 的插件更新都会记录在此文件中。

## \[1.5.0] - 2026-09-12

### Added

- 新增 Commit 通知「合并转发」推送模式（`forward_merge` 配置分组）：插件仍按原方式轮询配置的监听仓库，但本轮检测到的 commit 通知不再逐条发送，而是先逐条收集起来，随后按推送目标整合成一条（或按节点上限拆分的多条）合并转发聊天记录统一发送到对应的群 / 私聊，每个 commit 通知作为转发记录中的一个节点；仓库提交频繁时可有效避免刷屏
- 新增 `forward_merge.enabled`（默认关闭，关闭时维持原有的逐条发送行为）
- 新增 `forward_merge.content_mode`：`follow`（默认，跟随 `commit_output_format`，文字节点或图片卡片节点）/ `text`（一律文字节点）/ `image`（一律图片卡片节点，单节点渲染失败时自动回退为该节点的文字内容）
- 新增 `forward_merge.node_name` / `forward_merge.node_uin`：转发记录中每个节点展示用的昵称与账号（可填机器人自身 QQ 号）
- 新增 `forward_merge.max_nodes_per_message`（默认 20）：单条合并转发的最大节点数，超出后自动拆分为多条转发记录依次发送
- 新增 `forward_merge.fallback_to_normal`（默认开启）：qq_official / Telegram 等不支持合并转发的平台自动降级为逐条普通消息，保证不漏通知；关闭后这些目标会被判定为发送失败
- 同一批次内相同仓库的相同提交自动去重入队，避免上一轮整合发送中途异常时产生重复节点

### Changed

- commit 图片卡片渲染抽取为公共方法 `_render_commit_card_image`，普通推送与合并转发共用同一套渲染、异常捕获与失败回退逻辑
- 合并转发中发送失败的单个目标会写入待重试队列，下一轮轮询自动重试，其余目标不受影响

### Fixed

- 修复「合并转发」模式下通知可能永久丢失的问题：提交数据（`commits.json`）会在整合发送之前推进到最新 SHA，而待发送队列原先只存在于内存中，一旦进程崩溃、或整合发送过程中抛出未处理异常，该提交就会被记为已检查、队列里的通知却再也没有机会发出。现在队列在入队、发送前清空、写入重试队列时都会原子落盘（`pending_forward_notifications.json`，位于插件数据目录），插件启动时自动恢复上次未发送完的条目并在本轮重新入队补发
- 修复「合并转发」模式下发送失败的目标在下一轮 `retry_failed_notifications()` 中被当成普通提交通知逐条重试、把一条合并转发消息拆成多条普通消息的问题：失败载荷现在携带 `forward_merge` / `forward_batch` 元数据，重试时按推送目标重新合并为一个批次并走合并转发发送器，与首次发送保持一致（同一目标仍是一条转发记录，含 `max_nodes_per_message` 分片）
- commit 数据、已发送通知记录、Issues 快照 / 推送日志 / 监控状态、失败通知队列统一改为「写临时文件 + 原子替换」落盘，避免写入过程中进程退出留下截断的 JSON 导致整份记录丢失（新增 `utils/file_utils.py` 的 `write_json_atomic`）

## \[1.4.1] - 2026-09-05

### Added

- Issues 动态通知支持图片：新评论中的图片（GitHub 截图上传生成的 `<img src="...">` HTML 标签、markdown `![alt](url)` 语法、常见扩展名的裸图片直链）与通知文本合并为同一条消息发送（文字在前、图片紧随其后），正文中原位置替换为「[图片]」占位符；单条通知最多携带 6 张，图片下载/发送失败时自动降级为纯文本通知

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

