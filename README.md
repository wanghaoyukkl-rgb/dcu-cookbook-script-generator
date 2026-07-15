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
- 为生成脚本增加组和其他用户读写权限，为输出目录增加读取和遍历权限。
- 将生成结果同步到飞书 vLLM/SGLang 工作表并向个人或群聊推送消息。
- 同一框架下模型名和加速卡都相同时更新旧记录并清理历史重复项。

## 快速开始

### 1. 安装 skill

```bash
export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
git clone https://github.com/wanghaoyukkl-rgb/dcu-cookbook-script-generator.git \
  "$CODEX_HOME/skills/dcu-cookbook-script-generator"
```

重新打开 Codex 会话后，使用 `$dcu-cookbook-script-generator` 调用该 skill。

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

凭证必须放在 skill 和 Git 仓库之外：

```bash
mkdir -p "$HOME/.config/dcu-cookbook-script-generator"
chmod 700 "$HOME/.config/dcu-cookbook-script-generator"
```

以仓库中的 `feishu.example.json` 为脱敏格式参考，在仓库外创建 `$HOME/.config/dcu-cookbook-script-generator/feishu.json`：

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

```bash
chmod 600 "$HOME/.config/dcu-cookbook-script-generator/feishu.json"
```

使用 `chat_id` 时，机器人必须已加入目标群。首次写入新增记录；之后在同一框架工作表中同时匹配模型名和加速卡时，按最新脚本更新脚本绝对路径、时间戳与 KVCache-FP8，并清理重复项。详细规则见 `references/feishu_reporting.md`。

真实 `feishu.json` 不得打包进 skill 或提交到 GitHub。App Secret 是应用密码；公开分发它会使所有下载者获得该应用已授权的飞书能力。

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

## 生成约束

- 服务脚本必须来自单一 cookbook 条目，不混合多个方案。
- 仅允许适配 `HIP_VISIBLE_DEVICES`、本地模型绝对路径和服务端口。
- 可以删除 `--numa-node`，并省略 `rm`、`rm -rf`、`rmdir` 等清理命令。
- dtype、量化方式、TP/PP/DP、上下文长度、调度参数和 DCU 环境变量必须保持来源方案不变。
- 模型、量化、框架、卡型或卡数冲突时停止生成，并报告 blocker。

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
├── feishu.example.json
├── agents/
│   └── openai.yaml
├── references/
│   ├── feishu_reporting.md
│   └── script_generation_workflow.md
├── scripts/
│   ├── finalize_script_permissions.py
│   ├── match_cookbook_model.py
│   ├── report_to_feishu.py
│   └── update_cookbook_cache.py
└── tests/
    └── test_report_to_feishu.py
```
