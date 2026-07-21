# 飞书写入与机器人推送

生成并校验 serve 脚本后，使用 `scripts/report_to_feishu.py` 将摘要同步到飞书表格，再由机器人向指定接收者推送相同摘要。

## 运行位置分支

- **集群内直接调用 skill，未使用登录连接器**：在集群上执行 `report_to_feishu.py --script-path <absolute-path>`，读取集群当前 skill 的固定配置并在集群上完成表格写入和机器人推送。
- **从本地使用 `connect_cluster.py` 登录集群**：远端对每个通过校验的新脚本执行 `report_to_feishu.py --script-path <remote-absolute-path> --emit-remote-summary`，只输出带脚本 SHA-256 和权限证据的非敏感摘要。连接器必须添加 `--local-feishu-report`；SSH 成功后，它先在本机校验所有摘要，再通过 stdin 调用本机同一 skill 的 `report_to_feishu.py --summary-stdin` 完成真实写表和机器人推送。
- 分支选择以是否使用连接器为准，不猜测 hostname。`--summary-stdin` 和 `DCU_FEISHU_SUMMARY` 是连接器内部协议，不手工复制、编辑或从不可信来源粘贴。
- 本地登录分支不得把 `assets/feishu.json`、任何 `FEISHU_*` 值或访问令牌上传到集群。连接器会在启动 OpenSSH 与本机上报器时都清除子进程环境中的 `FEISHU_*`，防止 SSH `SendEnv` 转发。远端绝对路径原样写入表格，时间戳由实际调用飞书的一侧生成。
- 批量生成时，远端可以在同一次 SSH 中为多个脚本各输出一条摘要；连接器会先校验全部摘要，再在本地逐个上报。任一项失败都会令完整闭环失败，但不会再次 SSH 或触发第二次认证。

表格写入采用 upsert 语义，在对应框架工作表内以“模型名 + 加速卡”为联合键：

- 表格中不存在相同模型名和加速卡的记录时新增完整记录。
- 同时匹配模型名和加速卡时，按最新脚本更新“脚本绝对路径”“时间戳”和“KVCache-FP8”，保留原有模型名和加速卡。
- 如果历史追加操作留下了多条相同联合键记录，只保留并更新第一条，清理其余重复项。
- 不同用户、自定义输出目录、路径别名或文件名变化不会产生重复记录。
- 同一模型使用不同加速卡时分别保留；卡数不参与联合键匹配。

上报字段优先读取脚本元信息。兼容缺少元信息的旧脚本时，模型路径回退读取启动命令的 `--model-path`，框架和卡型回退读取固定格式文件名；仍无法确定字段时停止上报。

## 安全规则

- 公开仓库中的 `assets/feishu.json` 只能保留占位符；不得提交或推送真实 App ID、App Secret、接收者 ID 或表格 URL/token。
- 用户拉取 skill 后可直接在自己的本地副本中填写 `assets/feishu.json`。发现真实凭证进入 Git 提交、聊天或终端历史时，立即在飞书开发者后台重置。
- 输出错误时不得打印 App Secret 或 `tenant_access_token`。
- 访问令牌只保存在进程内，不得写入配置文件、生成脚本或日志。

## 表头

表格按以下顺序包含 5 列：

| 表头 | 值 | 多维表格字段类型 |
| --- | --- | --- |
| `模型名` | 脚本元信息 `model_path` 最后一个 `/` 后的字段 | 文本 |
| `脚本绝对路径` | 生成脚本的绝对路径 | 文本 |
| `加速卡` | 脚本元信息 `card` | 文本 |
| `时间戳` | 写入时的本地时间；多维表格使用毫秒时间戳 | 日期 |
| `KVCache-FP8` | `kvcache: kvcache_fp8` 或命令包含 `--kv-cache-dtype fp8...` | 复选框 |

电子表格的首行必须预先按以上顺序创建表头。多维表格必须预先创建同名字段并使用表中指定的字段类型。

## 环境变量

本节中的 `FEISHU_*` 配置只适用于**集群内直接调用**。本地登录分支会清除这些环境变量，必须把真实字段填入运行连接器的本机 skill 固定 `assets/feishu.json`；不得用环境变量替代，也不得把 JSON 复制到远端。

通用必填项：

```bash
export FEISHU_APP_ID='<new-app-id>'
export FEISHU_APP_SECRET='<new-app-secret>'
export FEISHU_RECIPIENT_ID='<open-id-or-email>'
export FEISHU_RECIPIENT_ID_TYPE='open_id'  # 也可使用 email、user_id、union_id、chat_id
```

推荐直接提供飞书表格 URL，脚本会识别 `/base/` 多维表格、`/sheets/` 电子表格，以及带 `sheet=` 参数的 `/wiki/` 电子表格：

```bash
export FEISHU_TABLE_URL='<direct-feishu-table-url>'
```

用户拉取 skill 后，通过安全渠道联系维护者取得真实字段，直接填写 skill 内的 `assets/feishu.json`：

```json
{
  "app_id": "<app-id>",
  "app_secret": "<app-secret>",
  "recipient_id": "<open-id>",
  "recipient_id_type": "open_id",
  "table_type": "sheets",
  "table_url": "<direct-feishu-table-url>"
}
```

上报器固定读取 `<skill-root>/assets/feishu.json`，不读取 `~/.config` 或 `FEISHU_CONFIG_FILE` 指定的其它文件。集群内直接调用时，现有字段级环境变量仍可临时覆盖 JSON 中的同名值，但不会改变配置文件路径；本地登录连接器的 `--local-feishu-report` 分支会主动移除所有 `FEISHU_*` 覆盖，只允许使用连接器同一份本机 skill 的固定 JSON。模板占位符未替换时，本地分支会在 SSH 前阻断，直接调用则会在调用飞书 API 前返回缺失配置异常。

`assets/feishu.json` 是 Git 跟踪文件。填写真实值后不得执行包含该文件的 `git add`、提交或推送；对外发布的版本必须始终恢复为占位符。

`/wiki/` 链接会通过知识库节点接口解析真实电子表格 token。应用需申请“查看知识空间节点信息”或“查看知识库”权限，并拥有该节点的阅读权限。

不使用 URL 时，多维表格配置为：

```bash
export FEISHU_TABLE_TYPE='bitable'
export FEISHU_BITABLE_APP_TOKEN='<app-token>'
export FEISHU_BITABLE_TABLE_ID='<table-id>'
```

电子表格配置为：

```bash
export FEISHU_TABLE_TYPE='sheets'
export FEISHU_SPREADSHEET_TOKEN='<spreadsheet-token>'
export FEISHU_SHEET_ID='<sheet-id>'
```

同一电子表格使用标题为 `vllm`、`sglang` 的两个工作表时，脚本读取 serve 脚本的 `framework` 元信息，查询工作表列表并按标题自动选择 `sheet_id`。这种场景只需配置公共表格链接：

```bash
export FEISHU_TABLE_TYPE='sheets'
export FEISHU_TABLE_URL='<common-/wiki/-or-/sheets/-url>'
```

工作表标题无法固定时，可以显式覆盖自动匹配：

```bash
export FEISHU_TABLE_TYPE='sheets'
export FEISHU_TABLE_URL='<common-/wiki/-or-/sheets/-url>'
export FEISHU_SHEET_ID_VLLM='<vllm-sheet-id>'
export FEISHU_SHEET_ID_SGLANG='<sglang-sheet-id>'
```

也可以分别设置完整链接 `FEISHU_TABLE_URL_VLLM` 和 `FEISHU_TABLE_URL_SGLANG`。优先级为框架专用 ID、通用 ID、同名工作表自动匹配、链接中的当前 `sheet=`。

## 执行顺序

### 集群内直接调用

1. 先按 cookbook 生成并校验 serve 脚本。
2. 执行 dry-run，确认派生字段：

```bash
python3 scripts/report_to_feishu.py \
  --script-path '<ABSOLUTE_SERVE_SCRIPT_PATH>' \
  --table-type sheets \
  --dry-run
```

3. 配置完整后执行真实写入和推送：

```bash
python3 scripts/report_to_feishu.py \
  --script-path '<ABSOLUTE_SERVE_SCRIPT_PATH>'
```

4. 只有命令返回 `status: reported`，并同时给出 table 与 message 结果，才汇报飞书闭环成功。`table.action` 为 `created` 表示首次新增，为 `updated` 表示已按“模型名 + 加速卡”更新路径、时间戳和 KVCache-FP8。
5. 表格写入或消息推送失败时，重新执行完整上报闭环；每次间隔 3 秒，最多重试 3 次。输出中的 `attempts` 是总执行次数，`retries` 是实际重试次数。
6. 初次执行加 3 次重试后仍未同时完成表格和消息推送时，命令返回退出码 `1` 与 `reporting did not close after 4 attempts` 异常。保留已生成脚本，不得声称飞书闭环完成。

### 本地登录集群

1. 本机准备一个 Python 3.6 兼容的远端工作流；它负责更新 cookbook、生成和校验脚本，并对每个新脚本执行：

```bash
python3 scripts/report_to_feishu.py \
  --script-path '<REMOTE_ABSOLUTE_SERVE_SCRIPT_PATH>' \
  --emit-remote-summary
```

2. 本机使用唯一的 stdout/result 文件运行连接器，并明确添加 `--local-feishu-report`。连接器会在认证前检查本机固定配置。
3. 远端工作流退出码非零时，不解析摘要、不调用飞书；原样保留失败结果。
4. 远端工作流退出码为零时，连接器要求至少一个、最多 64 个严格格式摘要，先对全部摘要运行本机无网络 dry-run；任何摘要无效时不产生飞书副作用。
5. 全部摘要有效后逐个执行本机真实上报；一项在 4 次尝试后仍失败时继续尝试其余项，并把成功项写入 `feishu_reports`、失败项写入 `feishu_failures`。只有结果文件 `status=completed`、`exit_code=0`，且 `feishu_reports` 数量与远端摘要数量一致，才汇报完整闭环成功。

## 权限要求

- 应用需要获取企业自建应用的 `tenant_access_token` 权限。
- 多维表格模式需要新增记录或编辑多维表格权限，并确保应用身份可编辑目标表格。
- 电子表格模式需要编辑电子表格权限，并确保应用身份可编辑目标表格。
- `/wiki/` 链接需要查看知识空间节点信息或查看知识库权限，并确保应用可读取对应节点。
- 机器人消息需要发送消息权限；接收用户必须位于机器人的可用范围内。
