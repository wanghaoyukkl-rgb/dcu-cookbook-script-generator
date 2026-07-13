---
name: dcu-cookbook-script-generator
description: 基于 HYGON-AI dcu-inference-cookbook 生成或校验 DCU vLLM/SGLang 模型服务启动脚本。当用户要求更新/拉取 GitHub cookbook、查询 cookbook 部署方案、匹配模型/卡型/框架组合、从 cookbook 条目创建 serve/test 脚本，或检查已有 DCU 服务脚本是否符合 cookbook 最佳实践时使用本 skill。
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
   - 生成脚本的输出路径
2. 在本 skill 根目录更新或查看 cookbook 缓存：
   - 默认检查：`python3 scripts/update_cookbook_cache.py --check`
   - 强制刷新 GitHub：`python3 scripts/update_cookbook_cache.py --force`
   - 仅查看状态：`python3 scripts/update_cookbook_cache.py --status`
3. 在 `~/cookbook/dcu-inference-cookbook/docs/model-deployment/<framework>/` 下定位 cookbook Markdown。
4. 匹配目标模型和过滤条件。对于表格型 cookbook 条目，优先使用 `scripts/match_cookbook_model.py`。
5. 只从一个来源生成服务脚本。不得混用多个 cookbook 条目、本地记录、历史脚本或用户片段。
6. 用来源条目反向校验生成脚本，并汇报来源、匹配状态和所有允许的适配项。

提取、匹配和写脚本的详细规则见 `references/script_generation_workflow.md`。

## 硬性规则

- Cookbook-first：读取部署方案前必须先更新或检查 HYGON-AI cookbook 缓存。
- 用户未提供模型路径时，先在 `/public/opendas/DL_DATA/llm-models/` 查找；没有有效匹配再依次查找 `/public2/opendas/DL_DATA/llm-models/`、`/public3/opendas/DL_DATA/llm-models/`、`/public4/opendas/DL_DATA/llm-models/`、`/module/`、`/module2/`、`/public4/share/` 和 `/parastor/opendas/DL_DATA/llm-models/`。选定后必须校验路径存在并记录 realpath。
- 生成脚本只允许做这些适配：设置 `HIP_VISIBLE_DEVICES`、把模型路径替换为目标节点绝对路径、必要时修改/新增服务监听端口、删除 `--numa-node ...`、省略 `rm`、`rm -rf`、`rmdir` 等清理命令。
- 保留来源方案里的 dtype、TP/PP/DP、量化参数、编译参数、调度参数、上下文长度、显存比例、MoE/通信变量和 DCU 专用环境变量。
- 如果模型路径缺失，或卡型/卡数/部署/量化与 cookbook 条目冲突，或来源缺少卡数、TP 等关键字段，不生成可执行脚本；标记 blocked，并要求用户提供来源脚本或修正输入。
- 低风险模糊匹配仅限基础模型身份一致，且差异只是 `instruct`、`thinking`、`0527`、`2507` 等非量化后缀。量化、框架、卡型、卡数和部署模式不一致都必须阻断。

## 脚本输出

生成脚本使用框架感知命名，例如：

```text
serve_vllm_<model>.sh
serve_sglang_<model>.sh
```

每个生成脚本开头必须包含元信息注释：模型、框架、cookbook 文件、cookbook 匹配、卡型、卡号、卡数、TP/PP/DP、部署模式、dtype、量化方式、KVCache、端口、模型路径、realpath（如果已知）和已做适配。

写入或检查脚本后，汇报：

- cookbook 缓存状态和 commit
- 唯一来源文件和匹配条目
- 精确匹配或模糊匹配状态
- 生成脚本路径
- 已做的允许适配
- blocker 或假设（如有）
