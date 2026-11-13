"""智能体主循环。

状态：只有接口。项目作者需要实现 ``run_loop``。
测试：``backend/tests/test_agent_loop.py``
这样跑目前会失败的测试::

    pytest -m student --runxfail tests/test_agent_loop.py

测试会注入一个假的模型。这个函数不能打开套接字，也不能导入大模型 SDK。

测试断言的 ``messages`` 形状如下::

    {"role": "user", "content": "<task text>"}
    {"role": "assistant", "content": "", "tool_name": "<name>", "tool_args": { ... }}
    {"role": "tool", "name": "<name>", "content": "<tool return value as str>"}
    {"role": "assistant", "content": "<final text>"}

工具结果如果本身是 ``str`` 就用返回值，否则用 ``str(value)``。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTurn:
    """一次脚本化的或真实的模型回复。

    设置 ``final_text`` 并且让 ``tool_name`` 保持 None，表示结束。
    设置 ``tool_name`` 表示要调用工具。``cost_usd`` 是这次回复的价格，
    必须 ``>= 0``。测试用 ``0.25`` 和 ``0.5`` 这样的值，因为这些小数在二进制
    浮点里是精确的。``0.1 + 0.2`` 不等于 ``0.3``；以后可以把单位改成整数的
    微美元。本 PR 仍用美元浮点，比较时用 ``>``。
    """

    cost_usd: float
    final_text: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None


@dataclass(frozen=True)
class LoopResult:
    """:func:`run_loop` 的结果。

    ``status`` 是 ``completed``、``step_limit``、``cost_limit``、
    ``denied``、``ask_human`` 或 ``error`` 之一。
    ``steps`` 统计真正执行过的模型补全次数，不含预先查看。
    ``cost_usd`` 是这些补全的费用之和。
    ``messages`` 是对话记录，包含最初的用户任务。
    """

    status: str
    steps: int
    cost_usd: float
    final_text: str | None
    messages: list[dict]


def run_loop(
    *,
    task: str,
    model: object,
    tools: dict[str, Callable],
    permissions: object,
    max_steps: int,
    max_cost_usd: float,
) -> LoopResult:
    """一直跑到智能体结束，或某个限制被触发。

    TODO(student): 换掉这个函数体。测试就是规格。建议按这个顺序：

    1. 若 ``max_steps < 1`` 或 ``max_cost_usd < 0``，在调用模型之前抛出
       ``ValueError``。
    2. ``messages`` 从 ``[{"role": "user", "content": task}]`` 开始。
    3. 每一轮按这个顺序：
       a. 若 ``steps >= max_steps``，返回 ``status="step_limit"``。不要再调用
          ``peek_cost`` 或 ``complete``。
       b. ``upcoming = float(model.peek_cost(messages))``。若
          ``spent + upcoming > max_cost_usd``，返回 ``status="cost_limit"``，
          并且不要调用 ``model.complete``。刚好花到预算是允许的；比较用的是
          ``>``，不是 ``>=``。
       c. ``turn = model.complete(messages)``。把 ``turn.cost_usd`` 加进
          ``spent``，步数加一。
       d. 若 ``turn.tool_name`` 是 None，追加
          ``{"role": "assistant", "content": turn.final_text}``，并带着这段
          最终文本返回 ``status="completed"``。若 ``final_text`` 也是 None，
          改为返回 ``status="error"``。
       e. 否则先追加模块文档字符串里那种助手工具消息，然后
          ``decision = permissions.check(name, args)``，其中 ``args`` 是
          ``dict(turn.tool_args or {})``。
          * ``Decision.DENY``：不要调用工具。返回 ``status="denied"``。
          * ``Decision.ASK_HUMAN``：不要调用工具。返回 ``status="ask_human"``。
          * ``Decision.ALLOW``：若 ``name`` 不在 ``tools`` 里，返回
            ``status="error"``，什么都不要调用。否则调用
            ``tools[name](**args)``，追加工具消息，然后继续循环。
          * 其他任何决定：返回 ``status="error"``。
    4. 不要接住工具抛出的异常，让它继续往外抛。
    5. ``Decision`` 定义在 ``devsquad.tools.permissions``。

    用 ``TraceRecorder`` 记下每一步是合适的后续工作。本 PR 的测试不要求这次
    写入，也不要求发起网络请求。

    抛出：
        NotImplementedError：在这个函数体被替换之前，每次都会抛出。
        ValueError：实现之后，步数上限不是正数，或费用上限为负时抛出。
    """
    raise NotImplementedError(
        "尚未实现 devsquad.agent.loop.run_loop"
        "（步数上限、费用上限、工具分发）。"
        "请看本函数文档字符串和 tests/test_agent_loop.py。"
        "运行：pytest -m student --runxfail tests/test_agent_loop.py"
    )
