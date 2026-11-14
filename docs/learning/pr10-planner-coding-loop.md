# PR10 学习笔记：编码循环与 Planner 代码检索

**一、编码循环**

1. **`run_loop` 的 Planner 开关**

   `run_loop` 位于 `backend/src/devsquad/agent/loop.py:72`。`planner_enabled=True` 时，它先检查步数、费用以及 `repo_root`、`planner_model`、`trace_recorder`、`task_id` 等必需参数，然后调用 `planner.plan` 生成计划，并记录 `run.plan` 事件。

   编码模型收到的初始上下文包含两条用户消息：原始任务，以及以“实施计划（含相关文件）”开头的计划文本。规划使用独立的 `planner_model`，步数和费用只统计后续编码阶段。当前实现中，关闭或省略 `planner_enabled` 会抛出 `NotImplementedError`，并不是运行普通编码循环；对应测试见 `backend/tests/test_planner_loop.py` 的 `test_disabled_preserves_legacy_behavior`。

2. **`_run_coding_loop` 的执行流程**

   `_run_coding_loop` 位于 `backend/src/devsquad/agent/loop.py:124`，每轮依次执行：

   - 在 `steps < max_steps` 时开始新一轮，否则返回 `step_limit`。
   - 调用 `model.peek_cost(messages)` 预估下一次费用；若 `spent + upcoming > max_cost_usd`，在调用模型前返回 `cost_limit`。恰好等于预算仍允许执行。
   - 调用 `model.complete(messages)`，然后累加实际费用并增加 `steps`。
   - 如果模型没有请求工具：有 `final_text` 就追加助手消息并返回 `completed`；连最终文本也没有则返回 `error`。
   - 如果模型请求工具：先记录一条带 `tool_name`、`tool_args` 的助手消息，再检查工具是否存在，并调用 `permissions.check`。拒绝、需要人工确认和非法权限结果分别得到 `denied`、`ask_human`、`error`。
   - 权限允许后才调用 `dispatch_tool`。成功结果会转换成字符串，追加为 `role="tool"` 的消息，再进入下一轮；`SandboxError` 返回 `denied`，其他执行异常返回 `error`，且不会把异常原文写入消息或 trace。

   一个容易忽略的细节是：达到步数上限只会阻止**下一次模型调用**。如果最后一次回复是工具调用，该工具仍会先执行，见 `backend/tests/test_coding_loop.py` 的 `test_step_limit_stops_before_next_model_call`。

3. **`dispatch_tool` 如何分派工具**

   `dispatch_tool` 位于 `backend/src/devsquad/tools/sandbox.py:13`：

   - 网络工具直接抛出 `SandboxError`。
   - 读写工具通过 `PathGuard` 检查并解析路径，再把绝对路径传给回调；`list_dir` 默认读取仓库根目录，写操作还会检查敏感路径。
   - `run_command` 不执行调用方提供的同名回调，而是固定创建 `CommandRunner(..., require_network_isolation=True)`，避免回调绕过网络隔离。
   - 其他自定义工具最终通过 `tools[name](**guarded)` 调用。

4. **为什么 Windows 上命令工具会被拒绝**

   `network_namespace_command` 在 `backend/src/devsquad/sandbox/network.py:139` 中只对 `sys.platform == "linux"` 探测网络命名空间，原生 Windows 会返回 `None`。随后，`CommandRunner.run` 在 `backend/src/devsquad/sandbox/runner.py:138` 发现无法隔离，而编码循环又强制要求隔离，于是抛出 `CommandRejected`，不会启动子进程。`_run_coding_loop` 将其记录为 `sandbox_denied` 并返回 `denied`。

   因此项目明确规定“Windows 上请在 WSL 里运行”（`README.md:100`）。但 WSL 不是成功保证：其中仍须存在 `unshare`，并且 Linux user/network namespace 探测成功，否则命令依然会被拒绝。

**二、Planner 接入代码检索**

1. **拆解任务前的关键词检索**

   `extract_keywords` 位于 `backend/src/devsquad/agent/planner.py:41`。它会拆分驼峰标识符、转小写，提取英文标识符和中文片段，为“检索”“规划”等常见中文术语补充英文关键词，最后保序去重并限制数量。

   `plan` 位于同文件 `:53`，将关键词拼成查询并截断，再调用：

   `search_code(root, query, keywords=..., top_k=..., backend="python")`

   `PlannerConfig` 控制命中数、关键词数、查询长度和片段长度，避免检索上下文无限增长。

2. **检索结果如何进入计划**

   每个命中会被转换为 `路径:起始行-结束行` 的文件引用，以及经过截断的代码片段。`plan` 把系统提示词、原始需求、文件引用和代码片段组成三条消息，交给 `planner_model.complete`。

   返回值由“模型生成的计划文本 + 检索来源及相关文件”组成；原始代码片段只进入 Planner 模型上下文，不会自动附在返回计划末尾。随后，`run_loop` 把完整计划写入 `run.plan`，并注入编码模型上下文。对应测试为 `backend/tests/test_planner.py` 的 `test_hits` 和 `backend/tests/test_planner_loop.py` 的 `test_loop_receives_plan_and_records_trace`。

3. **搜不到或出错时如何降级**

   检索正常但无命中时，`plan` 在相关文件部分加入“未检索到相关代码”，仍调用规划模型。检索或命中处理抛出异常时，则清空已经收集的片段和文件引用，按无命中继续规划，并且不会把异常原文传给模型或写入计划，见 `backend/tests/test_planner.py` 的 `test_no_hits`、`test_error`。

   降级只覆盖检索阶段：`planner_model.complete` 位于 `try/except` 之外，规划模型自身失败不会在这里被吞掉。
