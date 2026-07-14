# 飞书写入与机器人推送

生成并校验 serve 脚本后，使用 `scripts/report_to_feishu.py` 将摘要同步到飞书表格，再由机器人向指定接收者推送相同摘要。

表格写入采用 upsert 语义，在对应框架工作表内以“模型名 + 加速卡”为联合键：

- 表格中不存在相同模型名和加速卡的记录时新增完整记录。
- 同时匹配模型名和加速卡时，只更新“脚本绝对路径”和“时间戳”，保留原有模型名、加速卡和 KVCache-FP8。
- 如果历史追加操作留下了多条相同联合键记录，只保留并更新第一条，清理其余重复项。
- 不同用户、自定义输出目录、路径别名或文件名变化不会产生重复记录。
- 同一模型使用不同加速卡时分别保留；卡数不参与联合键匹配。

上报字段优先读取脚本元信息。兼容缺少元信息的旧脚本时，模型路径回退读取启动命令的 `--model-path`，框架和卡型回退读取固定格式文件名；仍无法确定字段时停止上报。

## 安全规则

- 不得把 App ID、App Secret、访问令牌、接收者 ID 或表格 token 写入 skill、生成脚本、Git 仓库或日志。
- App Secret 只允许通过 `FEISHU_APP_SECRET` 环境变量或 skill/Git 仓库外、权限为 `600` 的本机配置文件提供。发现凭证出现在聊天、终端历史或其它文件中时，先在飞书开发者后台重置。
- 输出错误时不得打印 App Secret 或 `tenant_access_token`。

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

本机长期使用同一表格时，可以把配置放在 `~/.config/dcu-cookbook-script-generator/feishu.json`，避免把资源 token 或凭证提交到 skill 仓库：

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

环境变量优先于本地配置文件。可以通过 `FEISHU_CONFIG_FILE` 指定其它配置文件路径。本机配置文件必须设为仅当前用户可读写（`chmod 600`），且不得加入 Git。包含 App Secret 时脚本会拒绝读取任何允许组用户或其他用户访问的配置文件。

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

4. 只有命令返回 `status: reported`，并同时给出 table 与 message 结果，才汇报飞书闭环成功。`table.action` 为 `created` 表示首次新增，为 `updated` 表示已按“模型名 + 加速卡”更新路径和时间戳。
5. 表格写入或消息推送失败时，保留已生成脚本，汇报具体阶段和错误；不得声称飞书闭环完成。

## 权限要求

- 应用需要获取企业自建应用的 `tenant_access_token` 权限。
- 多维表格模式需要新增记录或编辑多维表格权限，并确保应用身份可编辑目标表格。
- 电子表格模式需要编辑电子表格权限，并确保应用身份可编辑目标表格。
- `/wiki/` 链接需要查看知识空间节点信息或查看知识库权限，并确保应用可读取对应节点。
- 机器人消息需要发送消息权限；接收用户必须位于机器人的可用范围内。
