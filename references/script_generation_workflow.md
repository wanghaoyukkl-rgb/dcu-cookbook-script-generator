# Cookbook 脚本生成流程

当需要基于 HYGON-AI `dcu-inference-cookbook` 生成或校验 DCU vLLM/SGLang 服务脚本时，读取本参考。

## 1. 输入信息

生成脚本前先收集或推断以下字段：

- `model`：目标模型名和变体；如果是量化模型，必须包含量化后缀。
- `model_path`：目标节点可见的绝对路径。按本节定义的模型根目录优先级顺序查找。
- `framework`：`vllm` 或 `sglang`。
- `framework_version`：当用户或环境要求特定版本时，用作 cookbook 表格过滤条件。
- `card`：规范化卡型，例如 `BW1000`、`BW1100`、`BW1101` 或 `K100AI`。
- `cards`：用于 `HIP_VISIBLE_DEVICES` 的卡号，以及 `1x`、`2x`、`4x`、`8x` 等卡数。
- `deployment`：默认 `IFB`；只有用户明确要求时使用 `PD`。
- `quantization`：目标量化方式；没有量化后缀时按 BF16/未量化处理。
- `port`：如果来源方案已指定端口则沿用；否则 vLLM 默认 `8000`，SGLang 默认 `30000`，除非用户指定端口。
- `output_dir`：用户可选的脚本输出目录。只改变目录，不改变固定文件名。

如果用户未提供 `model_path`，先在默认根目录搜索：

```bash
find /public/opendas/DL_DATA/llm-models -maxdepth 4 \
  \( -type d -o -type l \) -iname '*<MODEL_KEYWORD>*' 2>/dev/null | head -20
```

默认根目录没有有效匹配时，依次搜索以下备用根目录：

```text
/public2/opendas/DL_DATA/llm-models
/public3/opendas/DL_DATA/llm-models
/public4/opendas/DL_DATA/llm-models
/module
/module2
/public4/share
/parastor/opendas/DL_DATA/llm-models
```

可以用以下命令按顺序收集候选：

```bash
for root in \
  /public/opendas/DL_DATA/llm-models \
  /public2/opendas/DL_DATA/llm-models \
  /public3/opendas/DL_DATA/llm-models \
  /public4/opendas/DL_DATA/llm-models \
  /module \
  /module2 \
  /public4/share \
  /parastor/opendas/DL_DATA/llm-models; do
  test -d "$root" || continue
  find "$root" -maxdepth 4 \
    \( -type d -o -type l \) -iname '*<MODEL_KEYWORD>*' 2>/dev/null
done
```

按根目录优先级选择第一个精确有效匹配。如果较早目录只有模糊匹配、相邻尺寸或不同量化变体，继续搜索所有备用根目录；不得因为目录名相似就提前选定。默认只校验目录存在并记录 realpath，不读取或校验 `config.json` 中的 `compression_config`、`quantization_config`、`quantization` 等量化声明；只有用户明确要求检查模型量化配置时才读取这些字段。

选定路径后校验并记录软链接真实落点：

```bash
test -e '<MODEL_PATH>' && echo OK:'<MODEL_PATH>' && readlink -f '<MODEL_PATH>'
```

需要本地绝对模型路径时，不要把 Hugging Face 或 ModelScope id 保留为可执行脚本里的模型路径。

## 2. 更新 Cookbook 缓存

读取 cookbook 文件前，在本 skill 根目录执行：

```bash
python3 scripts/update_cookbook_cache.py --check
```

用户要求更新、拉取、刷新或重新 clone cookbook 时使用 `--force`。用户只要求查看缓存状态时使用 `--status`。

默认缓存路径：

- 仓库：`~/cookbook/dcu-inference-cookbook`
- 状态文件：`~/cookbook/cookbook_state.json`
- 部署文档：`~/cookbook/dcu-inference-cookbook/docs/model-deployment`

如果缓存更新失败但本地缓存存在，可以在说明失败原因、缓存 commit 和日期后临时使用现有缓存。如果本地没有缓存，不要假装已经检查 cookbook；询问用户是否提供脚本或稍后重试。

## 3. 选择 Cookbook 文件

只选择一个 Markdown 文件：

- vLLM：`docs/model-deployment/vllm/`
- SGLang：`docs/model-deployment/sglang/`

常见模型族文件包括 `qwen3.md`、`qwen3.5.md`、`deepseek-v3.2.md`、`glm-5.md`、`kimi-k2.5.md`、`minimax-2.x.md` 和框架专属变体。

使用 `rg -n "<model-family>|<model-name>|BW1000|BW1100|K100|IFB|PD" <cookbook-file>` 定位候选段落，再读取匹配位置附近内容。

## 4. 匹配方案

对于表格型 cookbook 文档，运行：

```bash
python3 scripts/match_cookbook_model.py \
  --cookbook-file ~/cookbook/dcu-inference-cookbook/docs/model-deployment/<framework>/<family>.md \
  --model '<MODEL>' \
  --framework-version '<VERSION>' \
  --card '<CARD>' \
  --cards '<Nx>' \
  --deployment '<IFB|PD>' \
  --quantization '<QUANTIZATION>' \
  --top-k 3
```

只有确实未知的过滤条件才能省略。不得为了强行命中而省略已知的量化、卡型、卡数或部署模式。

可以自动接受：

- `status: exact`，且模型身份和所有硬过滤条件一致。
- `status: fuzzy`，但仅限基础身份一致，差异只是 `instruct`、`thinking`、`base`、`0527`、`2507` 等低风险非量化后缀。
- 仅当用户明确要求检查模型量化配置并要求建立受控别名时，才读取本地 `config.json`：例如把 `.w8a8` 简写映射到 `Channel-INT8-w8a8` 时，必须同时证明 weights 为 `num_bits=8`、`type=int`、`strategy=channel`，input activations 为 `num_bits=8`、`type=int`、`strategy=token`，并记录 `quantization_alias: config_verified`。默认不做该映射。

必须拒绝或阻断：

- 量化方式不一致，例如 BF16/未量化目标匹配到 `w8a8`、`int8`、`fp8`、`awq`、`channel-int8`、`channel-fp8` 等候选。
- 框架、卡型、卡数、部署模式、TP/PP/DP 或关键参数不一致。
- 候选来自不同模型族或相邻模型尺寸。
- 来源方案需要多节点或超过本地单机 8 卡，而用户没有明确提供对应环境。

使用低风险模糊匹配时，必须在脚本元信息中记录 `fuzzy_match: low_risk_suffix`、原始目标模型、候选条目和后缀差异。

## 5. 提取方案

只从单一选中的 cookbook 来源提取：

- DCU、NUMA、通信、量化、MoE、PD/IFB 和框架行为相关环境变量。
- 服务命令：vLLM 通常是 `vllm serve ...`，SGLang 通常是 `python3 -m sglang.launch_server ...`。
- 推荐卡型/卡数、TP/PP/DP、dtype、量化方式、KVCache、上下文长度、显存比例、编译参数、调度参数和部署模式。
- 来源方案中的服务端口（如果有）。

保持所选方案完整一致。不得把表格行、其它段落、本地测试指导、旧 `serve_*.sh` 或用户片段拼接起来，除非用户明确声明该片段就是唯一来源。

## 6. 最小适配

允许的适配：

- 添加或设置 `HIP_VISIBLE_DEVICES=<cards>`。
- 将模型路径或模型 id 替换为 `model_path`。
- 因用户要求或端口冲突，只修改服务监听端口。
- 删除所有 `--numa-node ...` 参数。
- 省略 `rm`、`rm -rf`、`rmdir` 等清理命令。

除非用户明确提供替代来源，否则禁止修改：

- dtype、量化方式、TP/PP/DP、上下文长度、显存比例、调度参数、`-cc` 等编译参数或框架优化开关。
- DCU、NUMA、通信、量化、MoE 或 PD/IFB 环境变量。
- 含义不明确的分布式主机/启动地址，例如 `<HOST_IP>`、`master_ip`、`NODE2_IP` 或 `--dist-init-addr`。

判定多节点时解析参数值而不是只检查参数名：`--nnodes 1` 与 `--node-rank 0` 是显式单节点配置，原样保留且不得阻断。`--nnodes` 大于 1、`--node-rank` 非 0、同一参数重复、值不是确定的整数，或存在未解析的外部节点地址时才标记 blocked。单独的本地端口参数不构成多节点证据。

来源缺少必需字段时标记 blocked。不得根据模型规模或当前空闲卡数推断 TP/卡数。

## 7. 写入脚本

先确定输出目录：

- 用户明确指定时，展开 `~` 并使用该目录；路径必须是绝对路径，不接受相对路径。
- 用户未指定时，使用 `~/cookbook/serve-scripts/<framework>-<framework-version>-single-node/`；框架版本未知时使用 `~/cookbook/serve-scripts/<framework>-single-node/`。
- 创建目录后仍按本节固定规则拼接文件名；不得把用户提供的目录当作自定义文件名。

先按以下固定格式生成文件名：

```text
serve_<framework>_<model>_<card>_<card-count>.sh
```

规范化规则：

- `framework` 使用小写 `vllm` 或 `sglang`。
- `model` 使用请求中的目标模型标识或模型 ID basename，不使用本地模型路径。转为小写，将空格、`/`、`_` 和其它非 `[a-z0-9.-]` 字符替换为 `-`，合并连续 `-` 并去掉首尾 `-`。
- `card` 先规范为标准卡型，再转为小写并移除 `_`、`-` 和空格。例如 `K100_AI`、`K100-AI` 均写为 `k100ai`。
- `card-count` 使用 cookbook 匹配条目的卡数并规范为 `<数字>x`；例如表格中的 `8` 和 `8x` 均写为 `8x`。
- 文件名不包含框架版本、端口、部署方式、`HIP_VISIBLE_DEVICES` 卡号或模型绝对路径。

示例：

```text
serve_sglang_qwen3-8b_bw1000_1x.sh
serve_sglang_kimi-k2.5_bw1100_8x.sh
serve_vllm_qwen3-8b-channel-int8-w8a8_k100ai_1x.sh
```

如果固定文件名已经存在，先读取其元信息。模型、框架、卡型、卡数或 cookbook 来源不同则停止写入并报告冲突；不得静默覆盖。相同目标需要更新时，也要在汇报中明确说明覆盖原因。

服务启动脚本使用 bash，并设置基础严格模式：

```bash
#!/usr/bin/env bash
set -euo pipefail
```

脚本开头写入元信息注释：

```bash
# generated_by: dcu-cookbook-script-generator
# source: HYGON-AI dcu-inference-cookbook
# cookbook_file: docs/model-deployment/<framework>/<family>.md
# cookbook_entry: <entry or heading>
# match: exact|fuzzy_low_risk_suffix
# model: <target model>
# framework: vllm|sglang
# card: <card type>
# cards: <card ids>
# card_count: <Nx>
# deployment: IFB|PD
# tp_pp_dp: TP=<n> PP=<n|unknown> DP=<n|unknown>
# dtype: <dtype>
# quantization: <quantization or none/bf16>
# kvcache: kvcache_fp8|default
# port: <port>
# model_path: <absolute path>
# model_realpath: <realpath or unknown>
# adaptations: HIP_VISIBLE_DEVICES, model_path, port, removed_numa_node, omitted_cleanup
```

随后写入环境变量和来源服务命令，只做允许的适配。为可读性，优先使用反斜杠拆分多行命令。

KVCache 判断规则：启动命令包含 `--kv-cache-dtype fp8...` 时，元信息 `kvcache` 写 `kvcache_fp8`；否则写 `default`。

写入或更新脚本后、内容校验前执行权限收尾：

```bash
python3 scripts/finalize_script_permissions.py \
  --script-path '<ABSOLUTE_SERVE_SCRIPT_PATH>'
```

该工具只增加权限，不移除已有权限：脚本增加 owner 执行权限和 `g/o+rw`；脚本所在输出目录增加 `g/o+rx`。目录的 `x` 是访问其中脚本所必需的遍历权限。不得递归修改输出目录的祖先路径。

## 8. 汇报前校验

检查生成脚本：

- 只命名了一个 cookbook 来源。
- 文件名符合 `serve_<framework>_<model>_<card>_<card-count>.sh`，且各字段与元信息一致。
- `HIP_VISIBLE_DEVICES` 与请求卡号一致。
- 模型路径是绝对路径；环境可访问时确认路径存在。
- 不包含 `rm`、`rm -rf`、`rmdir` 或 `--numa-node`。
- 元信息端口与命令端口一致。
- vLLM 命令只包含 vLLM 参数；SGLang 命令只包含 SGLang 参数。
- 来源方案中的测试设置没有被静默改写。
- 脚本权限包含 owner execute、group read/write、others read/write。
- 输出目录权限包含 group read/execute、others read/execute。

每个新建或更新的脚本通过以上检查后，立即读取 `references/feishu_reporting.md` 并按运行位置分支闭环，无需等待额外指令：集群内直接调用时在当前集群逐个真实上报；本地登录时在同一次 SSH 中逐个输出验证摘要，SSH 成功后由连接器使用本机配置逐个真实上报。仅查看或校验未发生变化的旧脚本不重复写入。最终汇报脚本路径，并简要说明来源、缓存 commit/日期、匹配状态、适配项、飞书写入/推送结果和 blocker。
