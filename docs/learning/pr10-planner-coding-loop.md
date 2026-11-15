# PR10 学习笔记：编码循环与 Planner 代码检索

本文对应 PR #10 的安全加固提交 `a23f741`，覆盖 Planner 开关、权限顺序、命令隔离和轨迹字段。

**一、编码循环**

1. **`run_loop` 的 Planner 开关**

   `run_loop` 位于 `backend/src/devsquad/agent/loop.py`。`planner_enabled=True` 时，它先检查步数、费用以及 `repo_root`、`planner_model`、`trace_recorder`、`task_id` 等必需参数，再调用 `planner.plan` 生成计划并记录检索与规划事件。

   编码模型收到的初始上下文包含两条用户消息：原始任务，以及以“实施计划（含相关文件）”开头的计划文本。规划使用独立的 `planner_model`，步数和费用只统计后续编码阶段。关闭或省略 `planner_enabled` 时，直接执行同一编码循环。此时无需提供 `planner_model`，初始上下文只有原始任务，也不会记录 `run.plan`。对应测试见 `test_disabled_runs_without_planning` 和 `test_disabled_completes_tool_round_trip_without_planner_model`。

2. **`_run_coding_loop` 的执行流程**

   `_run_coding_loop` 位于 `backend/src/devsquad/agent/loop.py`。每轮执行顺序如下。

   - 在 `steps < max_steps` 时开始新一轮，否则返回 `step_limit`。
   - 调用 `model.peek_cost(messages)` 预估下一次费用；若 `spent + upcoming > max_cost_usd`，在调用模型前返回 `cost_limit`。恰好等于预算仍允许执行。
   - 调用 `model.complete(messages)`，增加 `steps` 并累加实际费用。模型报告的实际费用超出预算时返回 `cost_limit`，本轮工具不会分发。
   - 如果模型没有请求工具：有 `final_text` 就追加助手消息并返回 `completed`；连最终文本也没有则返回 `error`。
   - 如果模型请求工具：先记录一条带 `tool_name`、`tool_args` 的助手消息，再检查工具是否存在，并调用 `permissions.check`。拒绝、需要人工确认和非法权限结果分别得到 `denied`、`ask_human`、`error`。权限检查本身抛出异常时也返回 `error`。这些分支都在工具分发前结束。
   - 权限允许后才调用 `dispatch_tool`。成功结果会转换成字符串，追加为 `role="tool"` 的消息，再进入下一轮；`SandboxError` 返回 `denied`，其他执行异常返回 `error`，且不会把异常原文写入消息或 trace。

   一个容易忽略的细节是：达到步数上限只会阻止**下一次模型调用**。如果最后一次回复是工具调用，该工具仍会先执行，见 `backend/tests/test_coding_loop.py` 的 `test_step_limit_stops_before_next_model_call`。

3. **`dispatch_tool` 如何分派工具**

   `dispatch_tool` 位于 `backend/src/devsquad/tools/sandbox.py:13`：

   - 网络工具直接抛出 `SandboxError`。
   - 读写工具通过 `PathGuard` 检查并解析路径，再把绝对路径传给回调；`list_dir` 默认读取仓库根目录，写操作还会检查敏感路径。
   - `run_command` 固定由 `CommandRunner` 执行，同时要求文件系统隔离和网络隔离。命令以普通参数列表进入 Bubblewrap，工作目录映射到只读的 `/workspace` 目录，临时文件写到私有 `/tmp`。
   - 其他自定义工具最终通过 `tools[name](**guarded)` 调用。

4. **为什么 Windows 上命令工具会被拒绝**

   Agent 命令要求 Linux 用户命名空间和可信的 Bubblewrap 安装。原生 Windows 缺少这条运行路径，WSL 中也必须满足命名空间与包装器的启动条件。普通源文件可以从快照读取，常见凭据路径、`.git`、符号链接和硬链接会在快照阶段排除。实测见 `backend/tests/test_filesystem_sandbox.py`。

**二、Planner 接入代码检索**

1. **拆解任务前的关键词检索**

   `extract_keywords` 位于 `backend/src/devsquad/agent/planner.py:41`。它会拆分驼峰标识符、转小写，提取英文标识符和中文片段，为“检索”“规划”等常见中文术语补充英文关键词，最后保序去重并限制数量。

   `plan` 位于同文件 `:53`，将关键词拼成查询并截断，再调用：

   `search_code(root, query, keywords=..., top_k=..., backend="python")`

   `PlannerConfig` 控制命中数、关键词数、查询长度和片段长度，避免检索上下文无限增长。

2. **检索结果如何进入计划**

   每个命中会被转换为 `路径:起始行-结束行` 的文件引用，以及经过截断的代码片段。`plan` 把系统提示词、原始需求、文件引用和代码片段组成三条消息，交给 `planner_model.complete`。

   返回值由模型生成的计划文本、检索来源和相关文件组成，并注入编码模型上下文。`run.retrieval` 记录命中数和是否降级，`run.plan` 记录耗时。对应测试为 `backend/tests/test_planner.py` 的 `test_hits` 和 `backend/tests/test_planner_loop.py` 的 `test_loop_receives_plan_and_records_trace`。

3. **搜不到或出错时如何降级**

   检索正常但无命中时，`plan` 在相关文件部分加入“未检索到相关代码”，仍调用规划模型。检索或命中处理抛出异常时，则清空已经收集的片段和文件引用，按无命中继续规划，并且不会把异常原文传给模型或写入计划，见 `backend/tests/test_planner.py` 的 `test_no_hits`、`test_error`。

   降级只覆盖检索阶段：`planner_model.complete` 位于 `try/except` 之外，规划模型自身失败不会在这里被吞掉。

## 验收小问答

1. Planner 关闭时，编码模型的初始消息是什么？会生成哪一种规划事件？
2. `peek_cost` 等于剩余预算时，是否会调用编码模型？步数用完时会怎样？
3. 检索异常与规划模型异常分别如何处理？为什么捕获异常的位置重要？
4. 权限结果为 `ASK_HUMAN` 时，工具是否已被分发？从哪里能核对顺序？
5. 命令进程可以修改哪些位置？哪些输入会在生成快照时排除？
