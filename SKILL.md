---
name: dcu-cookbook-script-generator
description: 基于 HYGON-AI dcu-inference-cookbook 生成或校验 DCU vLLM/SGLang 模型服务启动脚本，并在生成成功后自动将摘要写入飞书表格、通过机器人推送；仅在用户明确要求时支持从本地直连或双因子登录郑州集群。当用户要求更新/拉取 GitHub cookbook、查询 cookbook 部署方案、匹配模型/卡型/框架组合、从 cookbook 条目创建 serve/test 脚本、检查已有 DCU 服务脚本、接入飞书生成记录，或登录集群后执行这些操作时使用本 skill。
---

# DCU Cookbook 脚本生成器

本 skill 只处理 DCU LLM 服务脚本的 cookbook-to-script 闭环。它不创建容器、不启动服务、不运行 OpenCompass、不监控任务，也不做多模型排程；这些执行类任务交给更完整的 DCU 推理测试 pipeline skill。

## 本地登录集群（按需）

- 只有用户明确要求“登录集群”或“从本地连接集群”时，才读取 `references/local_cluster_login.md` 并建立 SSH 连接；普通 cookbook 或脚本请求不得自动登录。
- 默认目标为郑州集群。用户未指定登录方式时，让用户选择 `direct` 直连或 `2fa` 双因子。
- 直连由用户提供 IP，默认相关 VPN 已开启，端口默认 `22`。双因子固定使用 `42.228.13.241:65024`，由用户提供用户名。
- 密码和当前 30 秒验证码只允许在本机 OpenSSH 的交互提示中输入，不得写入聊天、命令行参数、环境变量或文件。
- 登录后要连续执行更新、生成、校验和飞书汇报时，必须把远端更新、生成、校验和摘要输出写成 UTF-8 工作流文件，并使用连接器的 `--script-file`、`--output-file`、`--result-file`、`--local-feishu-report` 在一次 SSH 会话内执行；飞书写入和机器人推送在 SSH 成功结束后由连接器调用本机配置完成。不得把多行工作流塞进 `--command` 或依赖嵌套引号。
- `--script-file` 默认按远端 Python 3.6 基线预检并执行。工作流必须使用 `universal_newlines=True` 和显式 `stdout/stderr=subprocess.PIPE`，不得使用 `text=`、`capture_output=`、`dataclasses`、`Path.is_relative_to()` 或 `Path.unlink(missing_ok=...)` 等更高版本接口。
- 登录成功后的 cookbook 更新、模型路径检查和脚本生成仍按下述原有流程执行，但执行位置在远端集群。

### 运行位置分支（硬规则）

- 只按本次流程是否使用 `scripts/connect_cluster.py` 选择分支，不通过 hostname、IP、目录或环境变量猜测运行位置。
- **集群内直接调用（不登录）**：生成和校验完成后，继续在当前集群环境执行 `python3 scripts/report_to_feishu.py --script-path <absolute-path>`；使用集群上该 skill 自己的 `assets/feishu.json` 完成表格 upsert 和机器人推送。
- **本地调用并登录集群**：远端只更新 cookbook、生成脚本、执行权限收尾和反向校验；每个通过校验的新脚本在远端执行 `python3 scripts/report_to_feishu.py --script-path <absolute-path> --emit-remote-summary`。该模式只输出不含凭据的 `DCU_FEISHU_SUMMARY`，不得在远端调用真实飞书 API。
- 本地登录分支必须给连接器添加 `--local-feishu-report`。连接器在认证前检查本机 skill 固定配置，SSH 成功后先严格校验本次返回的 1–64 个摘要，再逐个调用本机 `report_to_feishu.py --summary-stdin`。批量生成多个卡型时仍只建立一次 SSH；某项最终失败时继续尝试其余已校验项，并聚合全部成功/失败证据。
- App ID、App Secret、接收者 ID、表格 URL/token 和访问令牌永远不从本地同步到集群。远端脚本绝对路径保留为远端 POSIX 路径；写入时间戳由执行真实飞书写入的一侧生成。
- 本地登录分支只有远端工作流退出码为 `0`，且每个摘要的表格写入和机器人推送都成功，才算闭环完成。摘要或本机飞书失败后不得重新 SSH、不得要求用户再次认证；保留本机结果文件中的失败阶段和已完成证据。

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
7. 每个新建或更新的脚本通过校验后，必须立即读取 `references/feishu_reporting.md` 并按“运行位置分支”执行：集群内直接调用使用 `--script-path` 真实上报；本地登录分支在远端使用 `--script-path ... --emit-remote-summary`，再由连接器在本地使用 `--summary-stdin` 真实上报。不得等待用户再次要求。上报器按 `framework` 元信息选择 vLLM/SGLang 工作表，以“模型名 + 加速卡”为联合键新增或更新记录，再推送机器人消息。

提取、匹配和写脚本的详细规则见 `references/script_generation_workflow.md`。

## 硬性规则

- Cookbook-first：读取部署方案前必须先更新或检查 HYGON-AI cookbook 缓存。
- 用户未提供模型路径时，先在 `/public/opendas/DL_DATA/llm-models/` 查找；没有有效匹配再依次查找 `/public2/opendas/DL_DATA/llm-models/`、`/public3/opendas/DL_DATA/llm-models/`、`/public4/opendas/DL_DATA/llm-models/`、`/module/`、`/module2/`、`/public4/share/` 和 `/parastor/opendas/DL_DATA/llm-models/`。选定后必须校验路径存在并记录 realpath。
- 生成脚本只允许做这些适配：设置 `HIP_VISIBLE_DEVICES`、把模型路径替换为目标节点绝对路径、必要时修改/新增服务监听端口、删除 `--numa-node ...`、省略 `rm`、`rm -rf`、`rmdir` 等清理命令。
- 保留来源方案里的 dtype、TP/PP/DP、量化参数、编译参数、调度参数、上下文长度、显存比例、MoE/通信变量和 DCU 专用环境变量。
- 如果模型路径缺失，或卡型/卡数/部署/量化与 cookbook 条目冲突，或来源缺少卡数、TP 等关键字段，不生成可执行脚本；标记 blocked，并要求用户提供来源脚本或修正输入。
- 低风险模糊匹配仅限基础模型身份一致，且差异只是 `instruct`、`thinking`、`0527`、`2507` 等非量化后缀。量化、框架、卡型、卡数和部署模式不一致都必须阻断。
- 默认不得读取或校验 `config.json` 中的 `compression_config`、`quantization_config`、`quantization` 等量化声明；目标模型名（含量化后缀）与 cookbook 模型名、量化列精确一致时直接按 cookbook 生成。只有用户明确要求检查模型量化配置时才读取这些字段。模型简写无法精确命中时默认阻断并要求完整名称，不得为了匹配主动检查配置或建立量化别名。
- 判断来源是否要求多节点时必须解析参数值，不得仅因参数存在而阻断。`--nnodes 1` 和 `--node-rank 0` 明确表示单节点，必须原样保留；只有 `--nnodes` 大于 1、`--node-rank` 非 0、值重复或无法解析，或来源包含未解析的外部节点地址时才阻断。
- 公开仓库中的 `assets/feishu.json` 只能包含占位符。用户拉取 skill 并联系维护者取得真实字段后，直接填写自己本地 skill 副本中的该文件；上报器只从这个固定配置文件路径读取，不读取其它配置文件路径。真实 App Secret、接收者 ID 和表格 URL/token 不得提交或推送到 Git，也不得写入生成脚本或日志；访问令牌不得持久化。
- 本地登录分支只使用运行连接器的本机 skill 副本中的 `assets/feishu.json`；连接器启动 OpenSSH 与本机上报器时都移除子进程环境中的 `FEISHU_*`，防止环境覆盖或 SSH `SendEnv` 把信息带到远端。该配置不得上传、复制或安全同步到远端。集群内直接调用分支仍读取集群上当前 skill 副本的固定配置。
- 每个新建或更新且通过校验的 serve 脚本都必须自动执行一次飞书上报。仅查看或校验未改动的旧脚本时不得重复追加记录。上报失败时保留脚本，但整个闭环标记为 failed，不得声称任务完成。
- 飞书上报闭环失败时必须等待 3 秒后重新执行完整闭环，最多重试 3 次（初次执行加重试共最多 4 次）。最终仍未同时完成表格写入和机器人消息时，命令必须返回非零状态和明确异常，不得声称完成。
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
