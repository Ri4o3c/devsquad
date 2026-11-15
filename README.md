# DevSquad

DevSquad 是一个桌面端多智能体编程工作台。计划中的最终形态：

- 桌面壳：Tauri 2 + Vue 3 + TypeScript
- 本机 sidecar：Python FastAPI
- 智能体：LangGraph 上的 Planner、Coder、Tester、Reviewer

当前处于阶段 1。`devsquad run` 创建隔离工作区，并可在 Linux 或 WSL 显式启动模型编码循环。

## 阶段计划

### 阶段 1（正在做）

先把「一个任务一份隔离工作区」和安全基线立住，再写智能体。

| 内容 | 状态 |
| --- | --- |
| 仓库骨架、CI、SQLite 里的任务表和 trace 表 | 本 PR |
| `devsquad run` 在目标仓库外面建 git worktree | 本 PR |
| 路径守卫和命令运行器（参数列表、超时、最小环境变量） | 本 PR |
| FastAPI 只提供本机 `/health` | 本 PR |
| Agent 主循环、工具权限、BM25 检索 | 编码循环、BM25 检索和默认权限决策已实现 |
| 命令隔离 | Agent 命令使用 Bubblewrap 隔离文件系统、进程和网络 |
| DeepSeek CLI | 可选模型执行和逐次人工确认 |
| Vue 页面、Tauri 壳 | 后续 PR |

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

在 Linux 或 WSL 使用 `--model deepseek-flash` 启动编码循环。需要在本机进程环境配置 `DEEPSEEK_API_KEY`。开启后，任务文字和经过大小限制与常见凭据格式过滤的工具结果会发往 DeepSeek。仅对允许外发相关代码片段的仓库开启模型执行。

```bash
devsquad run --repo /path/to/repo --task "给 parser 补上空输入测试" \
  --model deepseek-flash --max-steps 8 --max-cost-usd 0.05
devsquad trace <task-id>
git -C /path/to/task-worktree diff
git -C /path/to/task-worktree status --short
```

模型可列目录、读取文本、创建文件、修改一个唯一匹配的文本片段或请求运行隔离命令。创建或修改文件以及运行命令时，终端会显示本次申请的参数，默认拒绝。拒绝后模型可继续。没有交互终端时，运行停在 `ask_human`。预算以表列高峰单价估算，模型调用超出估算预算时停止分发工具。

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

权限决策测试按普通用例执行。以下命令也可让曾标记 `xfail` 的学生用例按普通测试执行。

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

创建符号链接需要系统权限。Windows 上权限不足时可跳过相关测试，目录联接测试有独立覆盖。

Agent 的 `run_command` 要求支持用户命名空间的 Linux 和新版 Bubblewrap。默认使用 `/usr/bin/bwrap`。部署方可通过 `DEVSQUAD_BWRAP` 指定可信安装的绝对路径。缺少隔离能力时命令无法启动。

进程在 `/workspace` 读取只读快照，可写入私有 `/tmp`。快照按文件名排除常见凭据、Git 元数据和本地依赖缓存，并略过链接与特殊文件。快照最多 64 MiB 和 10,000 个目录项。命令使用 Linux 系统工具和运行库，支持以相对路径访问源文件。

默认权限策略允许安全路径的读取，对写操作和命令返回 `ASK_HUMAN`，交由 CLI 确认。单独使用 `CommandRunner` 的旧接口可选仅做 argv 和网络策略检查，隔离后的 Agent 工具入口固定走文件系统与网络隔离。

## 再往下读

- 隔离工作区为什么这样设计：`docs/adr/0001-worktree-isolation.md`
- 这一 PR 的短笔记：`docs/learning/pr01-skeleton-worktree-sandbox.md`
