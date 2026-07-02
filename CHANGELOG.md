# Changelog

所有 noteworthy 的插件更新都会记录在此文件中。

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

