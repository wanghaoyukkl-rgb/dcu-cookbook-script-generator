# DCU Cookbook 脚本生成器

这是一个 Codex skill，用于基于 [HYGON-AI/dcu-inference-cookbook](https://github.com/HYGON-AI/dcu-inference-cookbook) 生成或校验 DCU vLLM/SGLang 模型服务启动脚本。

它负责完成 cookbook 更新、部署方案匹配、模型路径定位、服务脚本生成和反向校验。它不会创建容器、启动服务、运行精度/性能测试或调度多模型任务。

## 功能

- 更新或检查本地 `dcu-inference-cookbook` 缓存。
- 按模型、框架版本、卡型、卡数、部署方式和量化方式匹配部署方案。
- 从单一 cookbook 条目生成 vLLM/SGLang serve 脚本。
- 按预设优先级在多个共享模型目录中定位模型权重。
- 校验已有脚本是否保留 cookbook 的关键参数和 DCU 环境变量。

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
5. 写入脚本并根据来源条目反向校验。

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

### 4. 手工匹配部署条目

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

## 项目结构

```text
.
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   └── script_generation_workflow.md
└── scripts/
    ├── match_cookbook_model.py
    └── update_cookbook_cache.py
```
