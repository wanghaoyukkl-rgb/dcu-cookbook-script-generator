# DCU Cookbook 脚本生成器

这是一个 Codex skill，用于基于 [HYGON-AI/dcu-inference-cookbook](https://github.com/HYGON-AI/dcu-inference-cookbook) 生成或校验 DCU vLLM/SGLang 模型服务启动脚本。

它负责完成 cookbook 更新、部署方案匹配、模型路径定位、服务脚本生成、权限收尾、反向校验和飞书汇报。它不会创建容器、启动服务、运行精度/性能测试或调度多模型任务。

## 功能

- 更新或检查本地 `dcu-inference-cookbook` 缓存。
- 按模型、框架版本、卡型、卡数、部署方式和量化方式匹配部署方案。
- 从单一 cookbook 条目生成 vLLM/SGLang serve 脚本。
- 按预设优先级在多个共享模型目录中定位模型权重。
- 校验已有脚本是否保留 cookbook 的关键参数和 DCU 环境变量。
- 支持用户指定脚本输出目录，未指定时使用默认目录。
- 为生成脚本保留共享编辑权限；常规新脚本权限为 `0766`，权限收尾只增加所需权限位，不移除已有权限。
- 将生成结果同步到飞书 vLLM/SGLang 工作表并向个人或群聊推送消息。
- 同一框架下模型名和加速卡都相同时更新旧记录并清理历史重复项。
- 仅在用户明确要求时，从本地直连或通过双因子登录郑州集群后执行原有流程。

## 快速开始

### 1. 安装 skill

```bash
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/wanghaoyukkl-rgb/dcu-cookbook-script-generator.git \
  "$CODEX_HOME/skills/dcu-cookbook-script-generator"
```

重新打开 Codex 会话后，使用 `$dcu-cookbook-script-generator` 调用该 skill。

### 本地登录集群（可选）

只有明确提出“登录集群”时才会建立连接。默认目标为郑州集群，支持：

- `direct`：用户提供直连 IP，默认所需 VPN 已开启，端口默认 `22`。
- `2fa`：固定连接 `42.228.13.241:65024`，用户提供用户名；密码和当前验证码仅在本机 OpenSSH 提示中输入。

在 Windows/Codex Desktop 中，由调用方在可见的 `cmd.exe` 窗口运行连接器，不使用 PowerShell 登录窗口。密码和 30 秒验证码只在 OpenSSH 提示出现后手工输入；连接器不会接收、保存或转发这些凭据。

更新 cookbook、生成脚本、权限收尾、反向校验和飞书汇报等多步骤流程，使用一个本机 UTF-8 工作流文件，并通过 `--script-file`、唯一的 `--output-file`/`--result-file` 和 `--local-feishu-report` 在一次 SSH 会话内完成。批量处理多个脚本也不会重复登录。远端只生成脚本并返回不含凭据的摘要；SSH 成功结束后，连接器再使用本机 skill 固定的 `assets/feishu.json` 写入飞书表格并推送机器人，飞书配置不会复制或同步到远端。本机 reporter 的机器 JSON 固定使用 UTF-8，不依赖 Windows 当前代码页。

连接器会区分“SSH 登录或连接失败”和“SSH 登录成功、但远端工作流失败”。关闭认证窗口会中断本次连接；连接器不会自动再开窗口要求第二次认证。

登录说明见 `references/local_cluster_login.md`，连接入口为 `scripts/connect_cluster.py`。

### 2. 生成服务脚本

可以直接向 Codex 提交类似请求：

```text
使用 $dcu-cookbook-script-generator，先更新 cookbook，然后为 Qwen3-8B
生成 BW1000 单卡、SGLang 0.5.10、IFB 部署的 serve 脚本。
模型路径为 /public/opendas/DL_DATA/llm-models/qwen3/Qwen3-8B。
```

skill 会依次执行：

1. 检查或更新 cookbook 缓存。
2. 匹配唯一部署条目。
3. 校验模型路径、卡型、卡数和量化方式。
4. 只做允许的路径、卡号和端口适配。
5. 写入脚本并完成文件、目录权限收尾。
6. 根据来源条目反向校验。
7. 更新飞书表格中的当前记录并推送机器人消息。

### 3. 手工检查 cookbook 缓存

```bash
cd "${CODEX_HOME:-$HOME/.codex}/skills/dcu-cookbook-script-generator"

# 按缓存有效期检查并更新
python3 scripts/update_cookbook_cache.py --check

# 强制从 GitHub 更新
python3 scripts/update_cookbook_cache.py --force

# 仅查看缓存状态
python3 scripts/update_cookbook_cache.py --status
```

默认缓存位置：

```text
~/cookbook/dcu-inference-cookbook
~/cookbook/cookbook_state.json
```

### 4. 配置飞书汇报（可选）

仓库在 `assets/feishu.json` 中打包了脱敏模板。通过安全渠道联系 skill 维护者获取真实字段，然后直接编辑当前 skill 副本中的这个文件：

```json
{
  "app_id": "<app-id>",
  "app_secret": "<app-secret>",
  "recipient_id": "<open-id-or-chat-id>",
  "recipient_id_type": "chat_id",
  "table_type": "sheets",
  "table_url": "<feishu-sheet-or-wiki-url>"
}
```

使用 `chat_id` 时，机器人必须已加入目标群。首次写入新增记录；之后在同一框架工作表中同时匹配模型名和加速卡时，按最新脚本更新脚本绝对路径、时间戳与 KVCache-FP8，并清理重复项。详细规则见 `references/feishu_reporting.md`。

上报器固定读取 `<skill-root>/assets/feishu.json`，不读取其它配置文件路径。未替换的占位符会触发明确异常。字段级环境变量仍可临时覆盖 JSON 中的值，但不会改变配置文件路径。

`assets/feishu.json` 是 Git 跟踪文件。填写真实值后不得提交或推送该文件；公开版本必须始终只保留占位符。App Secret 是应用密码，泄漏会使其他人获得该应用已授权的飞书能力。

飞书闭环失败时，上报器固定等待 3 秒后重试，最多重试 3 次。初次执行加重试共最多 4 次；仍未同时完成表格写入和机器人消息时，命令返回退出码 `1` 和明确异常。

### 5. 手工匹配部署条目

```bash
python3 scripts/match_cookbook_model.py \
  --cookbook-file "$HOME/cookbook/dcu-inference-cookbook/docs/model-deployment/sglang/qwen3.md" \
  --model Qwen3-8B \
  --framework-version 0.5.10 \
  --card BW1000 \
  --cards 1x \
  --deployment IFB \
  --quantization BF16 \
  --top-k 3
```

## 模型路径搜索顺序

用户未明确提供模型路径时，skill 按以下顺序搜索：

```text
/public/opendas/DL_DATA/llm-models
/public2/opendas/DL_DATA/llm-models
/public3/opendas/DL_DATA/llm-models
/public4/opendas/DL_DATA/llm-models
/module
/module2
/public4/share
/parastor/opendas/DL_DATA/llm-models
```

只有模型身份、尺寸和量化方式一致时才会选定候选路径。选定后还会检查路径是否存在并记录 `realpath`。

默认不读取或校验模型 `config.json` 中的 `compression_config`、`quantization_config`、`quantization` 等量化声明；模型名与 cookbook 条目精确一致时直接按 cookbook 生成。只有用户明确要求检查模型量化配置时，才读取这些字段。

## 生成约束

- 服务脚本必须来自单一 cookbook 条目，不混合多个方案。
- 仅允许适配 `HIP_VISIBLE_DEVICES`、本地模型绝对路径和服务端口。
- 可以删除 `--numa-node`，并省略 `rm`、`rm -rf`、`rmdir` 等清理命令。
- dtype、量化方式、TP/PP/DP、上下文长度、调度参数和 DCU 环境变量必须保持来源方案不变。
- `--nnodes 1` 和 `--node-rank 0` 是单节点配置，必须原样保留；只有节点数大于 `1`、节点序号非 `0`、值重复或无法解析，或存在未解析的外部节点地址时才按多节点阻断。
- 模型、量化、框架、卡型或卡数冲突时停止生成，并报告 blocker。

## 验证

在 skill 根目录运行单元测试：

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## 脚本命名

生成的 serve 脚本固定使用以下格式：

```text
serve_<framework>_<model>_<card>_<card-count>.sh
```

例如：

```text
serve_sglang_qwen3-8b_bw1000_1x.sh
serve_sglang_kimi-k2.5_bw1100_8x.sh
serve_vllm_qwen3-8b-channel-int8-w8a8_k100ai_1x.sh
```

文件名统一使用小写；模型名中的空格、斜杠和下划线会转换为 `-`；卡型使用无分隔符的规范名称；卡数统一写为 `<数字>x`。框架版本、端口、卡号和模型绝对路径不会进入文件名。

用户未指定输出目录时，脚本默认写入：

```text
~/cookbook/serve-scripts/<framework>-<framework-version>-single-node/
```

用户可以指定其它绝对路径或 `~` 开头的目录，但不能改变固定文件名。

## 项目结构

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── feishu.json
├── references/
│   ├── feishu_reporting.md
│   ├── local_cluster_login.md
│   └── script_generation_workflow.md
├── scripts/
│   ├── connect_cluster.py
│   ├── finalize_script_permissions.py
│   ├── match_cookbook_model.py
│   ├── report_to_feishu.py
│   └── update_cookbook_cache.py
└── tests/
    ├── test_connect_cluster.py
    └── test_report_to_feishu.py
```
