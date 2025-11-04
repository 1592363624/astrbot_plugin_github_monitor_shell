<div align="center">

![:shell](https://count.getloli.com/@github_monitor_shell?name=github_monitor_shell&theme=minecraft&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)


[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Shell-blue)](https://github.com/1592363624)

</div>

# 效果图

<img width="484" height="395" alt="Shell截图_20251024165627" src="https://github.com/user-attachments/assets/d20e0b0b-cdf5-465c-b673-7d808e66b1d1" />

# 手动触发指令

/github_status  手动触发监控检查

/github_monitor  查看监控状态

# 配置说明

除了原有的配置项，现在还支持：

- `group_notification_targets`: 群通知目标（群号列表），可以将通知发送到指定的群聊中

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