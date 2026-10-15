# WeBridge

Local WeChat MCP for Windows. Read conversations through WeFlow; send and forward through the native WeChat client.

Windows 微信本地 MCP 服务。为 Codex 等 MCP 客户端提供聊天记录、联系人、媒体、消息推送、发送和原生逐条转发。自动聊天与模型调度由独立的 [WeBot](https://github.com/benbaobaoshigemi/WeBot) 提供。

## 环境

- Windows 10/11，Python 3.11 或 3.12，推荐 3.12。
- 已登录、中文界面的 Windows 微信 4.x；桌面控件适配基于 4.1.15.13。
- 独立安装 [WeFlow](https://github.com/hicccc77/WeFlow)，启用本地 HTTP API。使用消息推送时还需启用 WeFlow 的消息推送。
- 发送需要可交互的 Windows 桌面。锁屏、界面改版或用户同时操作微信可能影响发送。

Python 3.13+ 尚不在安装范围内：桌面依赖 winsdk 的 Windows 轮子目前只覆盖到 3.12。

## 安装

```powershell
git clone https://github.com/benbaobaoshigemi/WeBridge.git
cd WeBridge
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe .
```

也可使用已安装的 Python 3.12 执行 `python -m venv .venv`，再运行 `.\.venv\Scripts\python.exe -m pip install .`。不需要继承系统包。

## 接入 MCP 客户端

服务使用 stdio，不额外开放网络端口。将可执行文件路径换成实际安装路径：

```toml
[mcp_servers.wechat]
command = 'C:\Projects\WeBridge\.venv\Scripts\python.exe'
args = ["-m", "webridge"]
startup_timeout_sec = 30
tool_timeout_sec = 900
```

只需要读取时，使用 `args = ["-m", "webridge", "--read-only"]`，不注册发送和转发工具。

```powershell
.\.venv\Scripts\python.exe -m webridge --help
.\.venv\Scripts\python.exe -m webridge --version
```

stdio 模式没有普通终端菜单，应由 MCP 客户端启动。修改安装或配置后重新连接 MCP。

## 工具

| 工具 | 能力 |
| --- | --- |
| `list_sessions` | 最近会话，支持名称或 ID 筛选 |
| `search_contacts` | 联系人与群检索 |
| `get_messages` | 分页、日期、关键词检索与媒体导出 |
| `list_files` | 文件消息和已下载文件的本地路径 |
| `search_all_messages` | 跨会话检索，返回各会话错误 |
| `export_chat` | 按时间正序导出 UTF-8 文本 |
| `get_group_members` | 群成员及群昵称，支持刷新缓存 |
| `wait_new_messages` | 等待消息，支持会话过滤、事件游标和重放 |
| `send_text` | 发送文字并回读确认 |
| `send_image` | 发送本地图片并回读确认 |
| `send_file` | 发送本地文件并回读确认 |
| `forward_messages` | 来源最近 200 条内文字的原生逐条转发 |

转发接受来源会话、消息 ID 数组和目标会话。仅支持正文能唯一定位的文字；不生成合并聊天记录，不拼接正文冒充原生转发。会话名或联系人名必须能唯一解析。

`sent` 表示执行了发送动作，`confirmed` 表示目标会话回读到新消息。图片确认依据新出现的自发图片，不比较像素。未确认时先核对微信，避免重发。

## 配置与本地数据

默认读取 `%APPDATA%\WeFlow\WeFlow-config.json`，复用本地地址和 Token。

| 参数或变量 | 用途 |
| --- | --- |
| `--config PATH` / `WEFLOW_CONFIG` | WeFlow 配置路径 |
| `--state-dir PATH` / `WEBRIDGE_STATE_DIR` | 本地运行数据目录 |
| `--read-only` | 只注册读取工具 |

默认导出目录为 `%LOCALAPPDATA%\WeBridge\exports`，桌面驱动工作目录为 `%LOCALAPPDATA%\WeBridge\desktop`。凭据、数据库、聊天记录和日志不属于源码。

导出单次最多 50000 条，`has_more=true` 时缩小日期范围。文件只匹配消息月份目录中已下载的文件。媒体是否可用取决于本地数据和 WeFlow 的解密能力。

消息推送忽略握手；默认合并首条后三秒内的消息，可调整 `batch_seconds`。保存返回的 `event_id` 可接续读取 WeFlow 保留的重放。WeFlow 推送不包含自发消息，因此发送使用目标会话回读确认。

## 桌面操作

发送与转发由独立进程执行，共用跨进程发送锁。发送前等待键鼠空闲三秒，最长九十秒；结束后恢复前台窗口、光标和常见剪贴板数据。OLE 虚拟文件流、旧式 GDI 图元和调色板不在恢复范围内。

桌面驱动使用 wechatauto-replica 的 UIAutomation。控件树未开放时，上游驱动可能改写微信进程中的 Qt 无障碍状态位以开启控件；这不是官方微信 API，也不保证账号免受风控。只读模式不调用桌面驱动。上游曾报告被要求重新登录的风控事件，见[项目说明](https://github.com/fanyuantaier/wechatauto-replica/blob/main/README_pypi.md)。

WeBridge 信任本机 MCP 客户端的调用授权。主人身份与群内权限由 WeBot 管理。

## 许可证

[MIT](LICENSE)。第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。微信、WeFlow、用户数据和授权凭据不随仓库分发。
