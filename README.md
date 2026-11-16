# DevSquad

DevSquad 是一个桌面端多智能体编程工作台。计划中的最终形态：

- 桌面壳：Tauri 2 + Vue 3 + TypeScript
- 本机 sidecar：Python FastAPI
- 智能体：LangGraph 上的 Planner、Coder、Tester、Reviewer

当前处于阶段 1。CLI 和本机网页都可在隔离工作区运行编码任务。网页实时显示运行轨迹，支持逐次审批工具操作和查看 diff。独立评估入口在合成仓库中验证真实模型的单步编辑。

## 阶段计划

### 阶段 1（正在做）

先把「一个任务一份隔离工作区」和安全基线立住，再写智能体。

| 内容 | 状态 |
| --- | --- |
| 仓库骨架、CI、SQLite 里的任务表和 trace 表 | 已实现 |
| 在目标仓库外创建任务 worktree | CLI 和网页共用同一实现 |
| 路径守卫和命令运行器（参数列表、超时、最小环境变量） | 已实现 |
| FastAPI 本机服务 | 提供健康检查、任务创建、实时轨迹、审批和 diff |
| Agent 主循环、工具权限、BM25 检索 | 编码循环、BM25 检索和默认权限决策已实现 |
| 命令隔离 | Agent 命令使用 Bubblewrap 隔离文件系统、进程和网络 |
| Vue 页面 | 评估回放与实时任务工作台 |
| DeepSeek 调用 | CLI、网页和合成仓库评估 |
| Tauri 壳与多角色协作 | 后续阶段 |

## 真实模型小评估

2026-11-15，以 `2ab03f8` 为基线并加入运行轨迹，使用 `deepseek-flash` 的非思考模式在两个合成 Python 仓库上运行 8 个用例，每个重复 3 次。每次创建独立 Git worktree，只准修改指定函数的一个返回表达式，隐藏断言检查结果和变更范围。

| 指标 | 结果 |
| --- | --- |
| 隐藏断言通过 | 24 / 24 次 |
| 服务端计量 | 输入 2,856 tokens，输出 282 tokens |
| 模型请求平均耗时 | 0.899 秒 |
| 费用 | 按官方高峰价和返回用量估算 ¥0.007968 |

费用来自公开单价与 API 用量，账户账单可能采用不同的时段折扣。这组结果覆盖合成任务的单表达式编辑。[DeepSeek 计价表](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

```bash
# 已安装 backend，且已在本机环境配置 DEEPSEEK_API_KEY
python evals/run_deepseek.py --work-dir /tmp/devsquad-eval --csv results.csv --replay replay.json
```

`devsquad trace <task-id>` 按事件顺序显示运行元数据。回放页可打开随仓库提供的合成样本或导入 `replay.json`，启动方式见 [前端说明](frontend/README.md)。

## 目录

- `backend/`：Python 包 `devsquad`（需要 Python 3.11+）
- `frontend/`：Vue 工作台和运行回放
- `docs/learning/`：学习笔记
- `docs/adr/`：架构决定
- `evals/`：合成用例与真实模型评估入口

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

`search` 在过滤后的工作区快照中查找代码，返回匹配文件、行号和片段。安装了 `rg` 时使用 ripgrep 召回，再用 BM25 排序。缺少 `rg` 时由 Python 召回。轨迹只记录工具名、命中数和耗时等元数据。

`run_tests` 默认使用 `python3 -m pytest -p no:cacheprovider`。当前目录的 `package.json` 配有测试脚本时，默认使用 `npm test`。也可提供参数数组和测试文件。终端或网页会显示最终命令，批准后在只读源码快照和隔离网络中执行。测试运行器和依赖需要在沙箱可见的系统路径下可用。

### 网页工作台

在 Linux 或 WSL 中配置 `DEVSQUAD_TOKEN` 和 `DEEPSEEK_API_KEY`，构建前端后从仓库根目录运行以下命令。打开本机地址 `http://127.0.0.1:8765`，切到「实时任务」并输入访问口令。完整步骤见[前端说明](frontend/README.md)。

```bash
devsquad serve --web-root frontend/dist
```

网页逐次显示待审批工具的参数，120 秒内未作决定则拒绝该操作。运行状态、轨迹和 diff 通过同源的本机 API 访问。网页可重连查看当前服务进程最近一次运行，已持久化的轨迹也可用 `devsquad trace` 查询。

2026-11-16 使用 Chrome 和 `deepseek-flash` 验收了两条合成任务。

- 网页审批流程完成一次单行编辑，页面展示隔离工作区的 diff。原仓库的文件和 Git 工作区状态通过核对。3 次调用共返回 809 个输入 tokens 和 90 个输出 tokens，估算 ¥0.002338。
- 模型从通过文件选择、拖放、粘贴加入的 Markdown、PDF 和截图中读出三个校验码。原仓库和任务工作区通过核对。1 次调用返回 557 个输入 tokens 和 43 个输出 tokens，估算 ¥0.001458。

费用按公开高峰价和 API 用量估算。[DeepSeek 计价表](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)

### 任务管理

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
- `DEVSQUAD_TOKEN`：本机服务的访问口令，用于网页连接和 API 请求头 `X-DevSquad-Token`

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

默认权限策略允许安全路径的读取，对写操作和命令返回 `ASK_HUMAN`，交由终端或网页确认。单独使用 `CommandRunner` 的旧接口可选仅做 argv 和网络策略检查，隔离后的 Agent 工具入口固定走文件系统与网络隔离。

## 再往下读

- 隔离工作区为什么这样设计：`docs/adr/0001-worktree-isolation.md`
- 这一 PR 的短笔记：`docs/learning/pr01-skeleton-worktree-sandbox.md`
