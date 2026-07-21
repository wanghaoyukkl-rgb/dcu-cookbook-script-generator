# 本地登录集群

只有用户明确要求“登录集群”“从本地连接集群”或等价操作时使用本参考。普通 cookbook 查询、更新或脚本生成请求不得自行建立 SSH 连接。

## 登录方式

默认目标为郑州集群。用户未指定方式时，让用户选择：

1. `direct`：用户提供直连 IP，默认相关 VPN 已开启。端口默认 `22`，用户名可选。
2. `2fa`：固定连接 `42.228.13.241:65024`。用户提供用户名，密码和当前 30 秒验证码在 OpenSSH 提示出现后输入。

双因子方式不得接受其它 IP 或端口。

## 凭据规则

- 不要在聊天中询问、复述或保存密码、验证码。
- 不要通过命令行参数、环境变量、标准输入管道或文件传递密码、验证码。
- 连接器启动 OpenSSH 和本机上报器时都会从子进程环境中移除全部 `FEISHU_*` 变量，避免用户 SSH 配置中的 `SendEnv` 把飞书信息带到远端；本地登录分支只认本机固定 JSON。
- 只允许用户在本机原生终端显示的 OpenSSH 提示中输入。
- 不使用 `sshpass`，不关闭主机密钥检查。首次连接可由用户核对指纹；主机密钥变化时停止。
- 窗口关闭或 SSH 结束后连接即失效；再次连接需要重新认证。

## 连接命令

在本 skill 根目录运行。Windows 上使用真实 Python 解释器，不使用 Microsoft Store 占位程序。

只验证直连登录：

```bash
python3 scripts/connect_cluster.py direct \
  --host '<DIRECT_IP>' \
  --user '<OPTIONAL_USER>' \
  --check
```

只验证双因子登录：

```bash
python3 scripts/connect_cluster.py 2fa \
  --user '<USERNAME>' \
  --check
```

登录后执行一个不含凭据的远端命令：

```bash
python3 scripts/connect_cluster.py 2fa \
  --user '<USERNAME>' \
  --command 'cd ~/.codex/skills/dcu-cookbook-script-generator && python3 scripts/update_cookbook_cache.py --force'
```

`--command` 只用于短小的单条命令。更新 cookbook、生成脚本、校验和飞书汇报等多步骤流程必须写入一个本机 UTF-8 工作流文件，并在一次认证中执行。远端工作流对每个通过校验的新脚本运行：

```bash
python3 scripts/report_to_feishu.py \
  --script-path '<REMOTE_ABSOLUTE_SERVE_SCRIPT_PATH>' \
  --emit-remote-summary
```

该命令只输出不含凭据的摘要，不调用飞书。随后从本机运行连接器：

```bash
python3 scripts/connect_cluster.py 2fa \
  --user '<USERNAME>' \
  --script-file '<ABSOLUTE_LOCAL_WORKFLOW.py>' \
  --output-file '<UNIQUE_LOCAL_STDOUT_FILE>' \
  --result-file '<UNIQUE_LOCAL_RESULT_FILE>' \
  --local-feishu-report
```

连接器会在开窗前完成以下预检：

- 将脚本压缩并编码成不含嵌套引号的远端管道，避免 Windows OpenSSH 丢失 Python 字符串引号。
- 默认以远端 Python 3.6 为兼容基线；已知不兼容语法或 `subprocess` 参数会在认证前阻断。使用 `universal_newlines=True`，不得使用 `text=True` 或 `capture_output=True`。
- 限制脚本原始大小和压缩后命令长度；脚本、输出、结果文件必须使用不同路径。
- `--local-feishu-report` 在认证前检查连接器同一份本机 skill 中固定 `assets/feishu.json` 的字段完整性、类型和表格 URL 格式，不会联网，也不会输出配置值；缺失、冲突或仍为占位符时在打开 SSH 前阻断。
- 认证提示继续显示在可见终端的 stderr；远端 stdout 才写入 `--output-file`，其中不得输出凭据。
- 调用本机 `report_to_feishu.py` 时强制机器 JSON 输出为 UTF-8，不依赖 Windows 当前代码页；连接器仍按 UTF-8 严格解析 stdout。
- SSH 返回 `0` 后，连接器从 stdout 读取至少 1 个、最多 64 个 `DCU_FEISHU_SUMMARY`，先在本机无网络校验全部摘要，再用本机固定配置逐个写表和推送机器人。批量卡型仍只使用这一次 SSH；某项最终失败时仍尝试其余已校验项，并在结果文件聚合成功与失败证据。
- SSH 结束后原子写入 `--result-file`。只有该文件的 `status=completed`、`exit_code=0`，且每个摘要都有成功的 `feishu_reports` 证据，才表示完整闭环成功；输出文件出现或增长不代表成功。

每次连接使用新的输出和结果文件名。窗口被关闭而没有结果文件时，视为连接中断，不自动新开第二个双因子窗口。

只有工作流确实是 Bash 时才显式添加 `--script-interpreter bash`；默认保持 Python。

省略 `--check`、`--command` 和 `--script-file` 时进入交互式 SSH shell。

在 Codex Desktop 等当前通道不具备真实 TTY 的环境中，Windows 上由调用方使用可见的本机 `cmd.exe` 窗口运行连接器，让密码和验证码提示直接显示给用户；不要使用 PowerShell 登录窗口。连接器自身不负责创建弹窗。登录成功后，cookbook 更新、模型路径检查、脚本生成、权限收尾和摘要生成在远端执行；飞书表格写入与机器人推送在 SSH 成功后使用本机 skill 配置执行。不得把本机飞书配置复制到远端。SSH 失败时停止，不得改成本机生成脚本来掩盖失败；摘要校验或本机飞书失败时也不得重新连接或触发第二次认证。
