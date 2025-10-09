# 🛡️ AstrBot 防撤回插件 (Anti-Revoke)

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-orange)

一个为 [**AstrBot**]([AstrBot](https://github.com/AstrBotDevs/AstrBot)) 设计的高可靠性防撤回插件。

---

**<div align="center">**    <h2>👀 BIG BROTHER IS WATCHING YOU!</h2> </div>
------------------------------------

## ⚠️ 注意事项

| 项目               | 描述                                                                                                                       |
| :----------------- | :------------------------------------------------------------------------------------------------------------------------- |
| **支持平台** | 仅适配 **`aiocqhttp`** 平台。                                                                                             |
| **监控范围** | 仅支持 **群聊** 消息的撤回监控。                                                                                      |
| **消息类型** | **仅支持转发** **纯文本 (Plain)** 和 **图片 (Image)** 消息内容。其他富媒体类型（如视频、文件）会被忽略。 |

## ⚙️ 配置说明

| 配置项                              | 类型          | 默认值  | 描述                                                         |
| :---------------------------------- | :------------ | :------ | :----------------------------------------------------------- |
| **`monitor_groups`**        | `list[str]` | `[]`  | 要监控撤回事件的群号列表。                                   |
| **`target_receivers`**      | `list[str]` | `[]`  | 接收防撤回提醒的目标**QQ 号**。                     |
| **`ignore_senders`**        | `list[str]` | `[]`  | 忽略这些 QQ 号的撤回消息，不会触发转发。             |
| **`cache_expiration_time`** | `int`       | `300` | 消息缓存时间，单位: 秒。超过此时间的消息，撤回后将无法恢复。 |

---

### 🙏 致谢

本项目在开发过程中，参考了 **`astrbot_plugin_anti_recall`** 项目的缓存和处理逻辑，特此感谢：

- **原始代码参考：** [astrbot_plugin_anti_recall](https://github.com/jiongjiongJOJO/astrbot\_plugin\_anti\_recall)

