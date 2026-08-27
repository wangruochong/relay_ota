# Relay OTA 构建中心

供团队动效、资源同学使用的轻量网页构建工具。服务端仅依赖 Python 3.9+ 标准库，适合直接部署到内部打包机。

## 已实现

- SQLite 本地账号与登录会话，密码使用随机盐 + PBKDF2-SHA256 保存
- TP1 / TP4 Job 首页和独立构建页
- 资源目录模糊搜索、搜索结果选择、多路径组合
- 单构建锁串行执行资源编译、Git 提交和 Jenkins OTA 触发
- 构建者、提交/执行时间、全部参数和日志留档
- 页面每 2 秒自动同步构建状态
- 任一步骤失败时立即中止，返回错误并生成本地失败记录

## 快速启动

首次使用先添加本地账号：

```bash
python3 server.py add-user your_name
```

然后启动服务：

```bash
export TP_CLIENT_ROOT=/absolute/path/to/TripeaksClient
export TP_RES_ROOT=/absolute/path/to/Resources
python3 server.py serve --host 0.0.0.0 --port 8765
```

浏览器访问 `http://打包机IP:8765`。数据库与构建日志会保存在 `data/` 下。

## 环境变量

| 环境变量 | 内容 |
| --- | --- |
| `TP_CLIENT_ROOT` | TP1/TP4 共用的项目 Git 根路径，必填 |
| `TP_RES_ROOT` | 资源目录模糊检索与路径校验的根路径，必填 |
| `JENKINS_USER` | Jenkins 用户名，可匿名触发时不填 |
| `JENKINS_API_TOKEN` | Jenkins API Token，与 `JENKINS_USER` 同时设置 |

## 构建流水线

TP1 与 TP4 使用同一份 `TP_CLIENT_ROOT` 工作区，但分别切换到：

- TP1：`tripeaks/beta`
- TP4：`tripeaks4p/beta`

每次构建会串行执行：

1. `git reset --hard`、`git clean -fd`，切换对应主分支并拉取最新代码。
2. 执行 `coffee compile.coffee res`，每条资源路径使用一个 `-d <path>` 参数。
3. 执行 `git add -A`、`git commit -m res` 并推送对应主分支。
4. 读取 Jenkins 参数定义，将 `branch` 固定为 `beta`、`alert` 固定为 `true`，其余布尔参数设为 `false`、其余参数设为空，然后调用 `buildWithParameters`。
5. 生成最终状态的本地构建记录和日志。

命令输出会合并保存到对应构建日志。步骤 1～4 任一步骤失败都会停止后续操作、生成失败记录，并将错误返回页面。

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
