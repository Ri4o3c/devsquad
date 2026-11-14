# DevSquad

DevSquad 是一个桌面端多智能体编程工作台。计划中的最终形态：

- 桌面壳：Tauri 2 + Vue 3 + TypeScript
- 本机 sidecar：Python FastAPI
- 智能体：LangGraph 上的 Planner、Coder、Tester、Reviewer

当前仓库是阶段 1 的骨架。它不会调用模型，也不会改你仓库里的文件。`devsquad run` 只创建隔离工作区。

## 阶段计划

### 阶段 1（正在做）

先把「一个任务一份隔离工作区」和安全基线立住，再写智能体。

| 内容 | 状态 |
| --- | --- |
| 仓库骨架、CI、SQLite 里的任务表和 trace 表 | 本 PR |
| `devsquad run` 在目标仓库外面建 git worktree | 本 PR |
| 路径守卫和命令运行器（参数列表、超时、最小环境变量） | 本 PR |
| FastAPI 只提供本机 `/health` | 本 PR |
| Agent 主循环、工具权限、BM25 检索 | 只留接口和测试，由作者实现 |
| 默认禁网（Linux namespace；不可用时降级为 argv 拦截） | 已实现，结果标记隔离级别 |
| Vue 页面、Tauri 壳、真正的模型调用 | 后续 PR |

这里不写通过率、耗时或其他还没测过的数字。

## 目录

- `backend/`：Python 包 `devsquad`（需要 Python 3.11+）
- `frontend/`：占位。Vue 页面以后再加
- `docs/learning/`：学习笔记
- `docs/adr/`：架构决定
- `evals/`：占位。评测以后再加

## 命令

创建任务只有一条命令。需求用 `--task` 选项，不是位置参数：

```bash
devsquad run --repo /path/to/repo --task "给 parser 补上空输入测试"
```

它会：

1. 在目标仓库外面新建分支 `devsquad/<task-id>` 和对应的 git worktree
2. 把任务记进 SQLite
3. 打印 `task_id` 和 `worktree`
4. 说明智能体执行要到后面的 PR 才有（这次不会跑 agent）

辅助命令：

```bash
devsquad task list
devsquad task cleanup <task-id>
devsquad task cleanup <task-id> --force
```

没有 `devsquad task new`。创建任务走 `run`。库内部对应的函数是 `devsquad.tasks.create_task`。

有未提交改动，或者分支上还有没进入当前 `HEAD` / `main` / `master` 的提交时，`cleanup` 会拒绝，除非加上 `--force`。`--force` 也不会删掉目标仓库自己的工作区，也不会删 `main` 这类分支。

## 环境变量

程序不会自动读取 `.env`。例子在仓库根目录的 `.env.example`。

- `DEVSQUAD_DB`：SQLite 路径，默认 `~/.devsquad/devsquad.db`
- `DEVSQUAD_WORKSPACES`：worktree 的父目录，默认 `~/.devsquad/workspaces`，必须在目标仓库外面
- `DEVSQUAD_TOKEN`：调用 `GET /health` 时放在请求头 `X-DevSquad-Token` 里

## 本地检查

需要 Git 和 Python 3.11 以上。

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

ruff check .
pytest
```

三份学生作业的测试现在会失败，但标了 `xfail`，所以默认的 `pytest` 仍然是绿的。想看真正的失败（实现做对之后应该通过）：

```bash
pytest -m student --runxfail
```

| 模块 | 文件 | 测试 |
| --- | --- | --- |
| Agent 主循环（步数上限、费用上限、工具分发） | `backend/src/devsquad/agent/loop.py` | `pytest -m student --runxfail tests/test_agent_loop.py` |
| 工具权限（allow / ask-human / deny） | `backend/src/devsquad/tools/permissions.py` | `pytest -m student --runxfail tests/test_permissions.py` |
| BM25 | `backend/src/devsquad/retrieval/bm25.py` | `pytest -m student --runxfail tests/test_bm25.py` |

接口文件里的 docstring 写了预期行为，测试是规格。

## 第一期在 Windows 上的限制

Windows 上请在 WSL 里运行。超时会结束子进程树：POSIX 用进程组信号，Windows 用 `taskkill /F /T`。路径守卫会跟着符号链接和目录联接解析，并按不区分大小写的方式判断是否还在 worktree 里。

创建符号链接本身要系统允许（开发者模式或 `SeCreateSymbolicLinkPrivilege`）。机器拒绝创建时，相关测试会跳过；这不是把 Windows 上的检查关掉。目录联接不需要那个权限。网络默认关闭：Linux 探测非特权 user + net namespace，成功时强制隔离，失败时降级为 argv 拦截（`POLICY_ONLY`）。Windows 第一期没有非特权网络隔离，防火墙规则/AppContainer 需要管理员权限或额外工作；macOS 同样降级。argv 拦截只能防误操作。`RunResult.network` 标记实际级别；`CommandRunner(…, require_network_isolation=True)` 在无法强制隔离时拒绝执行，`allow_network=True` 显式允许网络（`ALLOWED`）。

## 再往下读

- 隔离工作区为什么这样设计：`docs/adr/0001-worktree-isolation.md`
- 这一 PR 的短笔记：`docs/learning/pr01-skeleton-worktree-sandbox.md`
