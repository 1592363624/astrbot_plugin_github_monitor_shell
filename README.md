<div align="center">

![:shell](https://count.getloli.com/@github_monitor_shell?name=github_monitor_shell&theme=minecraft&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)


[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Shell-blue)](https://github.com/1592363624)

</div>

# 效果图

<img width="947" height="809" alt="Shell截图_20251104142553" src="https://github.com/user-attachments/assets/b10366c3-c6e6-4f0f-a77c-d6c122ac6611" />


# 手动触发指令

/github_status  手动触发监控检查

/github_monitor  查看监控状态

# 配置说明

除了原有的配置项，现在还支持：

- `group_notification_targets`: 群通知目标，可以将通知发送到指定的群聊中
- `time_zone`: 时间显示时区（默认 `Asia/Shanghai`，即可显示为北京时间）
- `time_format`: 时间显示格式，使用 Python `strftime` 语法，默认 `%Y-%m-%d %H:%M:%S`
- `forward_merge`: 合并转发推送，开启后本轮轮询检测到的 commit 通知会先收集、再整合成一条「合并转发」聊天记录发送

## 推送目标格式

`notification_targets`（私聊）、`group_notification_targets`（群聊）以及仓库配置中的 `groups` 支持两种格式：

### 传统格式

- 纯数字群号/QQ号，如 `123456`，自动匹配QQ系列平台（aiocqhttp / qq_official / qq_official_webhook）
- 以 `-` 开头的ID：Telegram 群组

### UMO 格式（推荐）

`平台ID:消息类型:会话ID`，平台无关且自带路由信息：

```
aiocqhttp:GroupMessage:123456
小爱同学:GroupMessage:0771687B325FC423AD9F4C06A88D84E3
qq_official:FriendMessage:0771687B325FC423AD9F4C06A88D84E3
```

QQ 官方机器人（qq_official / qq_official_webhook）没有传统数字群号/QQ号，只有一串十六进制的 openid 会话ID，必须使用 UMO 格式（或直接填写 openid，单平台部署时会自动匹配）。配置了多个 bot/平台时，UMO 可以明确指定由哪个平台的哪个会话接收推送。

> 自动匹配行为：直接填写目标 ID（不写 UMO）时，插件会按 ID 特征自动选择平台——纯数字 ID 优先匹配 aiocqhttp（OneBot）；非数字 ID（官方bot的十六进制 openid）优先匹配 qq_official / qq_official_webhook。因此即使多平台同时运行，直接填 openid 也能正确路由到官方 bot，无需手动关闭其他适配器。

## 仓库配置增强功能

现在支持为每个仓库单独配置通知群组：

### 字符串格式配置（推荐）

```json
"repositories": [
"owner/repo",
"owner/repo|123456|91219736"
]
```

### 字典格式配置

```json
"repositories": [
{
"owner": "owner",
"repo": "repo"
},
{
"owner": "owner",
"repo": "repo",
"groups": ["123456", "91219736"]
}
]
```

示例：

```json
"repositories": [
"1592363624/astrbot_plugin_github_monitor_shell",
"1592363624/astrbot_plugin_github_monitor_shell|123456789|91219736"
]
```

表示监控 1592363624/astrbot_plugin_github_monitor_shell 仓库，当第二个仓库有更新时，除了全局配置的群通知目标外，还会通知
123456789 和 91219736 群组。

## 文转图（Commit 图片卡片）

将 commit 更新通知渲染为图片卡片发送（依赖 AstrBot 的 T2I 文转图服务，请先在后台配置好）。以下选项均位于插件配置的「文转图（图片通知）」分组：

- `commit_output_format`: `text`（默认）或 `image`
- `commit_image_template`: 内置 `terminal`（默认）/ `github_dark` / `light` / `retro_term` / `miku` / `sakura` 6 套主题，或选 `random` 每次随机
- `enable_base64_image`: 图片传输使用 Base64 编码（默认开启；关闭则用文件路径，需协议端与 AstrBot 共享文件系统）
- `t2i_image_type` / `t2i_quality` / `t2i_scale`: 图片格式（png/jpeg）、质量、清晰度

### 模板预览

| github_dark | miku | sakura |
| :---: | :---: | :---: |
| ![github_dark](assets/preview_github_dark.png) | ![miku](assets/preview_miku.png) | ![sakura](assets/preview_sakura.png) |

### 自定义模板

把 `.html` 文件放入插件数据目录的 `templates/` 文件夹（通常 `AstrBot/data/plugin_data/GitHub监控插件/templates/`），文件名（不含后缀）即模板名，填到 `commit_image_template` 即可使用，也会加入 `random` 的随机池。模板为 Jinja2 语法，可用变量：`title`、`repo_name`、`repo_url`、`branch`（可为 `None`）、`commit_count`、`generated_at`、`commits`（每项含 `sha_short` / `message` / `author` / `time` / `url`）。

模板需完全自包含（CSS 内联、无外部资源），并在 `<head>` 加上 `<meta name="viewport" content="width=<卡片宽度>, height=10">` 让 T2I 按内容裁剪，可参考内置 `templates/<主题>/commit_card.html`。

### 群文件 / 群相册备份上传

图片通知发送成功后可同时备份上传（仅 aiocqhttp 平台的 QQ 群）：

- `enable_group_file_upload` + `group_file_folder`: 上传到群文件指定文件夹（不存在自动创建，留空为根目录）
- `enable_group_album_upload` + `group_album_name` + `group_album_strict_mode`: 上传到群相册（NapCat 扩展 API）；严格模式下找不到指定相册会放弃上传，防止误传


## 合并转发推送（Commit）

开启后，插件仍然按照原来的方式定时轮询所有配置的监听仓库；区别在于**本轮检测到的 commit 通知不再逐条发送**，而是先逐条收集起来，最后按推送目标整合成**一条「合并转发」聊天记录**发送到对应的群 / 私聊，每个 commit 通知作为转发记录里的一个节点。以下选项均位于插件配置的「合并转发推送（Commit）」分组：

- `enabled`: 是否启用合并转发（默认关闭，关闭时维持原本的逐条发送行为）
- `content_mode`: 转发节点内容形式
  - `follow`（默认）：跟随「文转图（图片通知）」分组中的 `commit_output_format`——`text` 生成文字节点，`image` 生成图片卡片节点
  - `text`：节点一律使用文字
  - `image`：节点一律使用图片卡片（需先配置好 T2I 文转图服务；单个节点渲染失败时自动回退为文字节点，不影响其他节点）
- `node_name` / `node_uin`: 转发记录中每个节点显示的昵称与账号（仅用于展示，建议把 `node_uin` 填成机器人自身的 QQ 号）
- `max_nodes_per_message`: 单条合并转发的最大节点数（默认 20），本轮收集到的 commit 超过该数量时会拆分为多条合并转发依次发送
- `fallback_to_normal`: 平台不支持合并转发时是否降级为普通消息（默认开启）

### 合并规则

1. 插件按原有逻辑轮询仓库，检测到新 commit 时只把通知放入待发送队列，不立即发送；
2. 本轮所有仓库检查完成后，按推送目标分组：同一个目标收到的所有 commit 通知合并为一条转发记录（节点顺序即检测顺序）；
3. 全局群通知目标、仓库专属群目标、私聊目标分别独立成组，互不影响；
4. 若某个目标发送失败，该目标会被写入待重试队列，下一轮轮询时自动重试，其余目标不受影响。

### 平台支持

合并转发（OneBot v11 的 `send_group_forward_msg` / `send_private_forward_msg`）目前**仅 aiocqhttp（NapCat / Lagrange 等 OneBot 实现）支持**：

- 向 aiocqhttp 目标推送时，发送合并转发聊天记录；
- 向 qq_official / qq_official_webhook / Telegram 等不支持合并转发的平台推送时，默认自动降级为逐条普通消息，保证不漏通知；把 `fallback_to_normal` 关掉则这些目标会被判定为发送失败并进入重试队列。

## 🐔 联系作者

- **反馈**：欢迎在 [GitHub Issues](https://github.com/1592363624/astrbot_plugin_zanwo_shell/issues) 提交问题或建议
QQ群:91219736
telegram:[巅峰阁](https://t.me/ShellDFG)
