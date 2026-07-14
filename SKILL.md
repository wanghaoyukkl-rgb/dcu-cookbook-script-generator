---
name: dcu-cookbook-script-generator
description: 基于 HYGON-AI dcu-inference-cookbook 生成或校验 DCU vLLM/SGLang 模型服务启动脚本，并在生成成功后自动将摘要写入飞书表格、通过机器人推送。当用户要求更新/拉取 GitHub cookbook、查询 cookbook 部署方案、匹配模型/卡型/框架组合、从 cookbook 条目创建 serve/test 脚本、检查已有 DCU 服务脚本，或接入飞书生成记录时使用本 skill。
---

# DCU Cookbook 脚本生成器

本 skill 只处理 DCU LLM 服务脚本的 cookbook-to-script 闭环。它不创建容器、不启动服务、不运行 OpenCompass、不监控任务，也不做多模型排程；这些执行类任务交给更完整的 DCU 推理测试 pipeline skill。

## 闭环流程

1. 收集必要输入：
   - 模型名和本地模型路径
   - 推理框架：`vllm` 或 `sglang`
   - 目标卡型、卡号和卡数
   - 部署模式：默认 `IFB`；只有用户明确要求时使用 `PD`
   - 量化/KVCache 要求、框架版本和指定端口
   - 可选的生成脚本输出目录；用户未指定时使用默认目录
2. 在本 skill 根目录更新或查看 cookbook 缓存：
   - 默认检查：`python3 scripts/update_cookbook_cache.py --check`
   - 强制刷新 GitHub：`python3 scripts/update_cookbook_cache.py --force`
   - 仅查看状态：`python3 scripts/update_cookbook_cache.py --status`
3. 在 `~/cookbook/dcu-inference-cookbook/docs/model-deployment/<framework>/` 下定位 cookbook Markdown。
4. 匹配目标模型和过滤条件。对于表格型 cookbook 条目，优先使用 `scripts/match_cookbook_model.py`。
5. 只从一个来源生成服务脚本。不得混用多个 cookbook 条目、本地记录、历史脚本或用户片段。
6. 写入脚本后执行 `python3 scripts/finalize_script_permissions.py --script-path <absolute-path>`，为脚本及其输出目录增加规定权限，再用来源条目反向校验。
7. 每个新建或更新的脚本通过校验后，必须立即读取 `references/feishu_reporting.md` 并执行 `scripts/report_to_feishu.py --script-path <absolute-path>`；不得等待用户再次要求。上报器按 `framework` 元信息选择 vLLM/SGLang 工作表，以“模型名 + 加速卡”为联合键新增或更新记录，再推送机器人消息。

提取、匹配和写脚本的详细规则见 `references/script_generation_workflow.md`。

## 硬性规则

- Cookbook-first：读取部署方案前必须先更新或检查 HYGON-AI cookbook 缓存。
- 用户未提供模型路径时，先在 `/public/opendas/DL_DATA/llm-models/` 查找；没有有效匹配再依次查找 `/public2/opendas/DL_DATA/llm-models/`、`/public3/opendas/DL_DATA/llm-models/`、`/public4/opendas/DL_DATA/llm-models/`、`/module/`、`/module2/`、`/public4/share/` 和 `/parastor/opendas/DL_DATA/llm-models/`。选定后必须校验路径存在并记录 realpath。
- 生成脚本只允许做这些适配：设置 `HIP_VISIBLE_DEVICES`、把模型路径替换为目标节点绝对路径、必要时修改/新增服务监听端口、删除 `--numa-node ...`、省略 `rm`、`rm -rf`、`rmdir` 等清理命令。
- 保留来源方案里的 dtype、TP/PP/DP、量化参数、编译参数、调度参数、上下文长度、显存比例、MoE/通信变量和 DCU 专用环境变量。
- 如果模型路径缺失，或卡型/卡数/部署/量化与 cookbook 条目冲突，或来源缺少卡数、TP 等关键字段，不生成可执行脚本；标记 blocked，并要求用户提供来源脚本或修正输入。
- 低风险模糊匹配仅限基础模型身份一致，且差异只是 `instruct`、`thinking`、`0527`、`2507` 等非量化后缀。量化、框架、卡型、卡数和部署模式不一致都必须阻断。
- 用户用 `.w8a8` 简写目标模型时，仅当本地 `config.json` 同时证明权重为 8-bit INT channel 策略、激活为 8-bit INT token 策略，才可映射到 cookbook 的 `Channel-INT8-w8a8` 名称；元信息必须记录目标名、cookbook 名和 `quantization_alias: config_verified`。这不是跨量化模糊匹配。
- 不得把飞书 App Secret、访问令牌、接收者 ID 或表格 token 写入 skill、生成脚本、Git 仓库或日志。允许从环境变量或权限为 `600` 且位于 skill/Git 仓库外的本机配置文件读取；访问令牌不得持久化。
- 每个新建或更新且通过校验的 serve 脚本都必须自动执行一次飞书上报。仅查看或校验未改动的旧脚本时不得重复追加记录。上报失败时保留脚本，但整个闭环标记为 failed，不得声称任务完成。
- 飞书表格必须在框架工作表内按“模型名 + 加速卡”维护唯一的当前记录：首次上报新增；联合键相同时按最新脚本更新脚本绝对路径、时间戳和 KVCache-FP8，并清理该联合键的历史重复项。不得使用文件名或绝对路径参与匹配，因为不同用户生成同一模型、同一卡型脚本时目录和文件名可能不同；同一模型使用不同加速卡时必须分别保留。
- 飞书配置缺失或 API 调用失败时，保留已生成脚本并将飞书汇报标记为 blocked/failed；只有表格写入和机器人消息都成功才汇报闭环完成。
- 用户可指定自定义输出目录，但不得用自定义路径改变固定文件名。自定义目录必须是绝对路径或以 `~` 开头；用户未指定时默认使用 `~/cookbook/serve-scripts/<framework>-<framework-version>-single-node/`，未指定框架版本时使用 `~/cookbook/serve-scripts/<framework>-single-node/`。
- 每个新建或更新的脚本在校验和飞书上报前都必须执行权限收尾：脚本增加 owner 执行权限和 `g/o` 读写权限，输出目录增加 `g/o` 读及遍历权限。权限收尾失败则闭环失败。

## 脚本输出

所有生成脚本必须使用唯一固定格式：

```text
serve_<framework>_<model>_<card>_<card-count>.sh
```

- `framework`：固定为小写 `vllm` 或 `sglang`。
- `model`：使用目标模型标识而不是本地路径；转为小写，将空格、`/`、`_` 和其它非 `[a-z0-9.-]` 字符替换为 `-`，合并连续 `-`。
- `card`：先规范为 `BW1000`、`BW1100`、`BW1101`、`K100AI` 等标准卡型，再转为小写并移除分隔符。
- `card-count`：固定为 `<数字>x`，例如 `1x`、`2x`、`4x`、`8x`，不得使用实际卡号列表。
- 不得把框架版本、端口、部署方式、卡号或模型绝对路径加入文件名。目标文件已存在时先核对元信息；来源或目标不一致时不得静默覆盖。

例如：`serve_sglang_qwen3-8b_bw1000_1x.sh`、`serve_sglang_kimi-k2.5_bw1100_8x.sh`。

每个生成脚本开头必须包含元信息注释：模型、框架、cookbook 文件、cookbook 匹配、卡型、卡号、卡数、TP/PP/DP、部署模式、dtype、量化方式、KVCache、端口、模型路径、realpath（如果已知）和已做适配。

写入或检查脚本后，汇报：

- cookbook 缓存状态和 commit
- 唯一来源文件和匹配条目
- 精确匹配或模糊匹配状态
- 生成脚本路径
- 已做的允许适配
- 飞书表格写入和机器人推送状态（启用时）
- blocker 或假设（如有）
