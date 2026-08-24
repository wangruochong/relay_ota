# Relay OTA 构建中心

供团队动效、资源同学使用的轻量网页构建工具。服务端仅依赖 Python 3.9+ 标准库，适合直接部署到内部打包机。

## 已实现

- SQLite 本地账号与登录会话，密码使用随机盐 + PBKDF2-SHA256 保存
- TP1 / TP4 Job 首页和独立构建页
- 资源目录模糊搜索、搜索结果选择、多路径组合
- 单构建槽队列，完整展示等待中、构建中、成功、失败状态
- 构建者、提交/执行时间、全部参数和日志留档
- 页面每 2 秒自动同步构建状态
- 可配置真实构建命令；未配置时默认运行 4 秒演示构建

## 快速启动

首次使用先添加本地账号：

```bash
python3 server.py add-user your_name
```

然后启动服务：

```bash
python3 server.py serve --host 0.0.0.0 --port 8765
```

浏览器访问 `http://打包机IP:8765`。数据库与构建日志会保存在 `data/` 下。

## Job 与真实构建命令

编辑 [jobs.json](./jobs.json) 可修改 Job、资源根目录、环境列表及构建命令。`command` 是参数数组，不经过 shell，例如：

```json
{
  "command": ["/usr/bin/python3", "/absolute/path/to/build_ota.py"]
}
```

构建脚本可读取以下环境变量：

| 环境变量 | 内容 |
| --- | --- |
| `OTA_JOB_ID` | Job ID，例如 `tp1` |
| `OTA_BUILD_NUMBER` | 当前 Job 的构建编号 |
| `OTA_RESOURCE_ROOT` | 配置的绝对资源根目录 |
| `OTA_RESOURCE_PATHS` | 用户所选路径的 JSON 数组 |
| `OTA_ENVIRONMENT` | 目标环境 |
| `OTA_VERSION` | 可选版本标识 |

命令的标准输出和错误输出会合并保存到对应构建日志。退出码为 `0` 时构建成功，其他退出码为失败。

## 运行测试

```bash
PYTHONPYCACHEPREFIX=/tmp/relay-pycache python3 -m unittest discover -s tests -v
node --check static/app.js
```

## 部署建议

- 仅在公司内网开放端口，并通过防火墙限制来源。
- 若需要跨公网访问，在前面增加 Nginx/Caddy HTTPS 反向代理。
- 将 `data/ota_tool.db` 与 `data/logs/` 纳入打包机备份。
- 真实构建脚本建议使用专用低权限系统账号运行。
