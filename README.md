# Relay OTA 构建中心

供团队动效、资源同学使用的轻量网页构建工具。服务端仅依赖 Python 3.9+ 标准库，适合直接部署到内部打包机。

## 已实现

- SQLite 本地账号与登录会话，密码使用随机盐 + PBKDF2-SHA256 保存
- TP1 / TP4 Job 首页和独立构建页
- 资源目录模糊搜索、搜索结果选择、多路径组合
- 单线程构建池串行执行资源编译、Git 提交、Jenkins OTA 触发及结果跟踪
- 构建者、提交/执行时间、全部参数和日志留档
- 页面每 2 秒自动同步构建状态；构建详情打开期间，每 2 秒刷新本地构建日志
- 提交请求后立即生成本地记录，后台失败时自动更新为失败状态并记录原因

## 快速启动

```bash
python3 server.py serve --host 0.0.0.0 --port 8765
```

## 访问方式

浏览器访问 `http://打包机IP:8765`。

## 账号管理

添加账号：

```bash
python3 server.py add-user your_name
```

删除账号：

```bash
python3 server.py delete-user your_name
```

删除账号会立即清除该账号的登录会话，但不会删除历史构建记录。删除过的用户名可以通过 `add-user` 重新添加并设置新密码。

## 环境变量

| 环境变量 | 内容 |
| --- | --- |
| `TP_CLIENT_ROOT` | TP1/TP4 共用的项目 Git 根路径，必填 |
| `TP_RES_ROOT` | 资源 Git 仓库根目录，必填；构建时会清理本地修改并更新 `master` |
| `JENKINS_USER` | Jenkins 用户名，可匿名触发时不填 |
| `JENKINS_API_TOKEN` | Jenkins API Token，与 `JENKINS_USER` 同时设置 |

## 构建流水线

TP1 与 TP4 使用同一份 `TP_CLIENT_ROOT` 工作区，但分别切换到：

- TP1：`tripeaks/beta`
- TP4：`tripeaks4p/beta`

资源搜索范围从 `TP_RES_ROOT` 拼接项目子目录得到，接口返回值均相对于对应项目资源根路径：

- TP1：`ResourcesTripeasks_B/Resources`
- TP4：`ResourcesTripeasks4P/Resources`

每次构建会串行执行：

1. 在 `TP_RES_ROOT` 资源仓库中自动清理未被进程占用的残留 `index.lock`，执行 `git reset --hard`、`git clean -fd`，然后切换到 `master` 并拉取远端最新资源。任一 Git 命令遇到 `index.lock` 冲突时，会安全清理或等待占用进程，并最多自动重试 5 次。
2. 在 `TP_CLIENT_ROOT` 客户端仓库中执行相同的锁清理、`git reset --hard` 和 `git clean -fd`，递归清理现有子模块修改，然后切换对应项目主分支并拉取最新代码。
3. 执行 `git submodule sync --recursive` 和 `git submodule update --init --recursive --force`，再递归清理子模块中的本地修改与未跟踪文件。
4. 执行 `coffee compile.coffee res`，每条资源路径使用一个 `-d <path>` 参数；服务端会为旧版脚本补充非 TTY 输出兼容方法。
5. 执行 `git add -A`，以 `res_bot(登录用户名) <res_bot@local>` 作为 author 提交并推送对应主分支（例如 `res_bot(tester)`）；构建说明非空时提交信息为 `res:{构建说明}`，否则为 `res`。
6. 读取 Jenkins 参数定义，将 `branch` 固定为 `beta`、`alert` 固定为 `true`，其余布尔参数设为 `false`、其余参数设为空，然后调用 `buildWithParameters`。
7. 轮询 Jenkins 队列和实际构建（自动修正 Jenkins 返回的 localhost 地址，并对临时查询失败进行重试），保存 OTA 版本号，直到获得最终结果并更新本地记录和日志。

点击开始构建时会立即生成构建记录，并在当前项目页面左侧的构建列表中显示，同时清空构建表单。构建槽空闲时记录显示为“构建中”；已有任务正在执行时，新记录显示为“等待中”，由单线程构建池依次处理。命令输出会合并保存到对应构建日志，任一步骤失败都会停止后续操作，并把记录更新为失败状态。

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

## 注意事项

* 启动服务前，需要先设置以下两个环境变量：

  - `TP_CLIENT_ROOT`：TP1/TP4 共用的客户端项目 Git 根路径

  - `TP_RES_ROOT`：资源 Git 仓库根路径

* 数据库与构建日志会保存在 `data/` 下。
