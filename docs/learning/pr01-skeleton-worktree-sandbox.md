# PR 1 学习笔记：项目骨架、worktree 与安全基线

老板，这份笔记对应 PR 1 当前分支的源码、测试、README 和 ADR。建议先读本文，再按文中的路径去看 diff。最重要的一点先说：**这个 PR 只搭建控制面和安全基线；`devsquad run` 不会调用模型，也不会运行 Agent，只创建任务、分支、worktree 和数据库记录。**

文中行号对应当前 PR 分支；后续改代码可能让行号漂移，所以同时给出函数名。

## 1. 这个 PR 做了什么

### 1.1 目录结构

```text
.
├── .github/workflows/ci.yml          # GitHub Actions：Ruff + pytest
├── .env.example                     # 环境变量示例，不会被自动加载
├── backend/
│   ├── pyproject.toml               # 包元数据、CLI 入口、依赖、Ruff、pytest
│   ├── src/devsquad/
│   │   ├── __main__.py              # python -m devsquad
│   │   ├── cli.py                   # run / task list / task cleanup
│   │   ├── config.py                # DB、workspaces、token 的环境配置
│   │   ├── tasks.py                 # 任务记录、Git worktree 创建与清理
│   │   ├── db.py                    # SQLite tasks / traces 表
│   │   ├── traces.py                # 追加式执行事件记录
│   │   ├── server.py                # 仅本机使用的 GET /health
│   │   ├── sandbox/
│   │   │   ├── pathguard.py         # 路径、符号链接、.git、密钥文件名检查
│   │   │   └── runner.py            # argv 子进程、cwd、环境、超时、输出上限
│   │   ├── agent/loop.py            # Agent 主循环：编码循环 + 可选 Planner
│   │   ├── tools/permissions.py     # 留给老板实现：工具权限决策
│   │   └── retrieval/bm25.py        # 留给老板实现：BM25
│   └── tests/                       # 已实现功能的测试 + 三份学生规格测试
├── docs/adr/0001-worktree-isolation.md
├── docs/learning/pr01-skeleton-worktree-sandbox.md
├── frontend/README.md               # 仅占位，无 Vue/Tauri 实现
└── evals/README.md                  # 仅占位，无数据集或评测结果
```

### 1.2 按模块看交付范围

| 模块 | 本 PR 的实际状态 |
| --- | --- |
| Python 项目骨架 | 已完成。Python 3.11+，Hatchling 打包，Typer CLI，FastAPI sidecar。 |
| `devsquad run` | 已完成“创建任务”部分；不运行 Agent。需求必须通过 `--task` 传入。 |
| `devsquad task list` | 已完成。按创建时间、ID 排序，用制表符展示任务。 |
| `devsquad task cleanup` | 已完成。默认保护脏工作区和未合并提交，`--force` 可丢弃任务改动，但仍不能删用户主检出或异常分支。 |
| worktree 隔离 | 已完成。一个任务一条 `devsquad/<task-id>` 分支、一个仓库外目录。 |
| SQLite 任务表 | 已完成。保存任务、路径、状态、标题、出发 commit 等。 |
| 路径校验 | 已完成基线。规范化 `..`、解析符号链接、检查目录边界，禁写 `.git`，拦截密钥形状的读取路径。 |
| 命令运行器 | 已完成基线。只收 argv，固定 `shell=False`，限制 cwd、环境变量、超时和输出大小，超时杀进程组。 |
| 本地 HTTP 接口 | 已完成且只有 `GET /health`；校验 token、Host、Origin，固定绑定 `127.0.0.1`。 |
| 执行记录 | 已完成存储 API `TraceRecorder`；主循环在提供记录器和任务 ID 时记录 `loop.result` 与工具事件，开启规划时另记 `run.plan`。 |
| 三份学生作业 | BM25 已由 PR #4 实现；`run_loop` 已实现默认编码循环；仅 `PermissionPolicy.check` 的核心函数仍抛 `NotImplementedError`。 |
| CI | 已完成。Python 3.11 上安装、Ruff、默认 pytest。学生测试用非严格 `xfail` 暂时保持默认套件绿色。 |
| 前端、桌面壳、评测 | 只有目录说明，没有实现。 |

不存在的能力不要从目录名推断出来：没有 `task new`，没有 `devsquad serve` 命令，没有 Planner/Coder/Tester/Reviewer 的实际编排，也没有模型 SDK 调用。

## 2. 模块拆解：概念、设计、备选和坑

### 2.1 打包、配置与 CLI 入口

**概念与实现**

- `backend/pyproject.toml:19-20` 的 `[project.scripts]` 把可执行命令 `devsquad` 指向 `devsquad.cli:main`；`main()` 在 `backend/src/devsquad/cli.py:128` 调用 Typer `app`。
- `backend/src/devsquad/__main__.py:1-4` 让 `python -m devsquad` 复用同一个 `main()`，不是第二套入口。
- `run_command()`（`backend/src/devsquad/cli.py:101`）接收 `--repo` 和 `--task`；`task_list()`（同文件 `:61`）和 `task_cleanup()`（`:72`）挂在 `task` 子命令组下。
- `config.py` 的 `db_path()`（`:13`）、`workspaces_dir()`（`:21`）、`server_token()`（`:34`）每次调用都读环境变量。默认值分别是 `~/.devsquad/devsquad.db`、`~/.devsquad/workspaces` 和空 token。
- CLI 只把 `TaskError` 转成 stderr 和退出码 1；底层异常类型与命令行表示分开。

**为什么这样设计**

- 创建任务只保留 `run` 一个公开入口，避免 `task new` 与 `run` 两套创建语义漂移。`backend/tests/test_tasks.py:64` 的 `test_task_new_is_not_a_command` 明确把“不存在”写成规格。
- 每次读环境而不是 import 时固化，测试可以用 `monkeypatch` 改配置，不必重启进程；见 `backend/tests/conftest.py:39` 的 `cli_env`。
- `.env.example` 只是模板；`.gitignore:9` 忽略真实 `.env`，避免误提交。

**考虑过的备选**

- 位置参数需求：未采用。`test_run_rejects_positional_requirement`（`backend/tests/test_tasks.py:76`）要求拒绝，必须用 `--task`。
- 自动加载 `.env`：未采用。`config.py:1-5` 和 `README.md:63-69` 明确说明不自动读取，shell 环境才是输入。

**坑**

- `run_command()` 末尾明确打印 `agent_execution: deferred`；不要把“创建 worktree 成功”理解成“Agent 已执行”。
- 本 PR 没有把 `server.serve()` 注册为 CLI 命令，不能凭空写 `devsquad serve`。
- 使用 `/health` 前必须自己设置 `DEVSQUAD_TOKEN`；空配置会 fail closed。

### 2.2 任务、worktree 隔离与安全清理

**概念与实现**

核心在 `backend/src/devsquad/tasks.py`：

1. `create_task()`（`:69`）去掉标题首尾空白并拒绝空标题。
2. `_require_git_repo()`（`:264`）用 `git rev-parse` 确认仓库、找到顶层目录，并拒绝没有任何 commit 的仓库。
3. 记录当前 `HEAD` 为 `base_commit`，生成 12 位十六进制任务 ID（`new_task_id()`，`:64`），分支固定为 `devsquad/<id>`。
4. worktree 路径固定为 `<workspaces_dir>/<task-id>`；`_assert_outside_repo()`（`:278`）拒绝目标目录与仓库相同、互为祖先的情况。
5. `git(repo_root, "worktree", "add", "-b", branch, str(worktree), "HEAD")`（`:102`）从当前 HEAD 创建分支和 linked worktree。
6. 构造不可变 `Task` dataclass（`:50`），再由 `TaskStore.insert()`（`:179`）写入 SQLite。
7. 如果数据库写入失败，`_rollback_worktree()`（`:341`）尝试删除刚建的 worktree 和分支，避免只建一半。

linked worktree 中的 `.git` 通常是**文件**，指向主仓库的 Git 元数据，不是一份独立 `.git` 目录。`test_run_creates_worktree_outside_repo_and_records_row`（`backend/tests/test_tasks.py:82`）同时检查这一点、原仓库分支不变和数据库字段。

`cleanup_task()`（`tasks.py:124`）的顺序是：读任务、检查是否已清理、`_assert_removable()`（`:288`）、检查脏状态与合并状态、删 worktree、删分支、把数据库状态改成 `cleaned`。

- `_is_dirty()`（`:301`）看 `git status --porcelain`，所以未跟踪文件也算脏。
- `_is_merged()`（`:312`）依次检查任务分支是否已是 `HEAD`、`main`、`master` 的祖先；空任务分支本来就从 HEAD 出发，可以直接清理。
- `_remove_worktree()`（`:330`）在目录已消失时执行 `git worktree prune`，否则执行 `git worktree remove`。
- `_assert_removable()` 不受 `force` 控制：分支名必须匹配任务 ID，路径不能是目标仓库、仓库内部或仓库的祖先。
- 清理不是删除数据库行，而是保留记录并标记 `cleaned`，因此已有 traces 仍可保留。

**为什么这样设计**

用户当前打开的工作区可能有未提交修改。直接切分支会改变用户正在看的文件，或让任务修改与用户修改混在一起。worktree 共享 Git 对象库，却提供独立目录和分支；任务 diff、审查、并行与删除都有明确边界。完整决策见 `docs/adr/0001-worktree-isolation.md`。

**考虑过的备选**

ADR 明确比较了四种方案：

| 备选 | 不选的原因 |
| --- | --- |
| 在用户当前检出里换分支 | 干扰未提交文件，也不利于多个任务并行。 |
| 每个任务完整 clone | 更慢，复制对象库，与原仓库关系更远。 |
| 只复制文件 | 没有天然的可审查提交，清理和回滚都要另造机制。 |
| 每个任务一个容器 | 容器解决进程/资源隔离，不自动解决“改动落在哪条分支”；本 PR 先解决版本隔离。 |

**坑**

- `force=True` 只放宽“脏工作区/未合并提交”保护，不放宽路径和分支名保护。对应测试：`test_cleanup_refuses_to_delete_the_main_checkout`、`test_cleanup_refuses_worktree_path_that_is_the_repo`（`backend/tests/test_tasks.py:258`、`:281`）。
- 控制面 `git()`（`tasks.py:236`）也使用 argv，但它复制父环境，只额外设置 `GIT_TERMINAL_PROMPT=0`（`_git_env()`，`:257`）；它与 Agent 命令的 `CommandRunner.minimal_env()` 不是同一层，不要混淆。
- 数据库插入失败时的回滚是 best effort：两个回滚命令都用 `check=False`，没有再次抛出清理错误。
- 正常清理先动 Git 再更新 DB；如果最后的 `mark_cleaned()` 失败，代码没有把 Git 操作反向恢复的事务。这是阅读实现时应识别的边界，不要把它描述成跨 Git/SQLite 的原子事务。
- `_assert_outside_repo()` 使用解析后的路径判断，但 `workspaces.mkdir()` 在它之前执行；拒绝创建时可能已留下空的 workspaces 父目录。

### 2.3 `PathGuard`：路径、`.git` 与密钥文件

**概念与实现**

位置：`backend/src/devsquad/sandbox/pathguard.py`。

- 构造器 `PathGuard.__init__()`（`:80`）要求 root 已存在且是目录，并先 `resolve()` 固定真实根目录。
- `_candidate()`（`:124`）把相对路径拼到 root；绝对路径保持绝对路径，随后仍需通过边界检查。
- `_resolve_inside()`（`:129`）使用 `resolve(strict=False)`：规范化 `.`/`..`、展开已存在的符号链接，同时允许待创建的新文件；再用 `Path.relative_to(self.root)` 做路径段级别的包含判断。
- `check_read()`（`:88`）先查越界，再查密钥文件名；`check_write()`（`:105`）先查越界，再查 `.git`。
- `_touches_secret()`（`:155`）同时检查调用者原始路径和解析后路径的每一级名称；`_touches_git()`（`:160`）也检查这两侧。
- `is_secret_filename()`（`:65`）大小写不敏感，匹配 `.env*`、私钥、证书/密钥库等 glob；`.env.example`、`.env.sample`、`.env.template` 明确放行。
- 读 `.git` 允许，写 `.git` 抛 `GitWriteError`；越界抛 `PathEscapeError`，密钥读取抛 `SecretPathError`。

**为什么这样设计**

字符串前缀不等于目录包含：`/tmp/wt-evil/x` 以 `/tmp/wt` 开头，却在 root 外。符号链接又会改变真实目标，所以必须比较解析后的路径。

密钥检查也不能只看最终路径：`.env -> ordinary.txt` 会隐藏原始敏感名字，`notes.txt -> id_rsa` 会隐藏真实敏感目标。两侧都检查才能覆盖这两种方向。禁写 `.git` 则是因为 linked worktree 的 Git 指针与主仓库元数据相连，不能把它当普通项目文件。

**考虑过的备选**

- `str(path).startswith(str(root))`：被共享前缀兄弟目录绕过，测试 `test_rejects_sibling_directory_with_shared_prefix`（`backend/tests/test_pathguard.py:114`）专门覆盖。
- 只检查原始文件名：漏掉普通名字指向密钥的链接。
- 只检查 resolve 后文件名：漏掉敏感名字指向普通文件的链接。
- 在 `PathGuard.check_write()` 里直接禁密钥写入：本 PR 没这么做；docstring `:113-116` 明确把这条规则留给权限策略。

**坑**

- `PathGuard` 是应用调用前的检查，不是内核沙箱。检查与后续 `open()` 之间有 TOCTOU（time-of-check/time-of-use）窗口，符号链接可能在检查后被替换。
- 子进程可以绕过 `PathGuard` 自己 `open()` 任意绝对路径。
- `check_write()` 本身**不拦截** `.env` 等密钥写入；老板实现 `PermissionPolicy.check()` 时必须补上。
- 规则按名字而非内容识别密钥，可能误伤如 `id_rsa.pub`，也识别不了内容敏感但名字普通的文件；这是当前基线的明确粒度。

### 2.4 `CommandRunner`：argv、最小环境、超时与输出上限

**概念与实现**

位置：`backend/src/devsquad/sandbox/runner.py`。

- `_validate_args()`（`:232`）拒绝字符串/bytes、空序列、非字符串参数和含 NUL 的参数。
- `CommandRunner.run()`（`:135`）在 `subprocess.Popen` 中写死 `shell=False`（`:164`），没有打开 shell 的参数。`&&` 在 argv 中只是普通参数，不会变成第二条 shell 命令。
- `_cwd()`（`:218`）默认 worktree 根目录；指定 cwd 时要通过 `PathGuard.check_read()`，且必须是目录。
- `minimal_env()`（`:99`）只复制 `_ENV_ALLOW` 中的变量，并再用 `looks_like_secret_env()`（`:93`）检查名字；默认补 `LANG=C.UTF-8`。
- `stdin=DEVNULL`，stdout/stderr 分别用管道和读取线程 `_pump()`（`:249`）收集。每路保留的字节数受 `max_output_bytes` 限制，超限会设置 `truncated` 并停止子进程。
- `start_new_session=True` 创建新会话/进程组；超时或输出超限时，`_stop_process_group()`（`:270`）用 `os.killpg(..., SIGKILL)` 杀整组，而不是只杀直接子进程。
- 返回不可变 `RunResult`（`:75`），包含 argv、返回码、bytes 输出、是否超时、是否截断和耗时。

**为什么这样设计**

- shell 字符串会把数据与 shell 语法混在一起；argv 让进程直接收到参数，降低命令注入风险。
- 不复制整个父环境，避免把模型 API key、GitHub token、云凭证和 DevSquad 自己的 token 交给子进程。
- 只杀直接子进程可能留下孙进程继续运行，所以超时必须处理进程组。
- 管道一直不读可能让子进程阻塞；两个读取线程同时消费 stdout/stderr，并限制内存中的输出量。

**考虑过的备选**

- `shell=True` + 一整条命令字符串：明确不采用，源码扫描测试还禁止包内出现 `shell=True`。
- 全量继承 `os.environ`：会泄露父进程凭证，不采用。
- 只调用 `proc.kill()`：可能留下后代进程，不作为首选。
- 用 `HTTP_PROXY`/`NO_PROXY` 假装禁网：模块 docstring `:15-19` 明确反对，因为程序可以忽略代理变量直接 socket 连接。

**坑**

- `shell=False` 只解决 shell 解释问题，不会阻止被执行程序自身做危险操作。例如 argv 启动的 Python 仍能打开仓库外文件或联网。
- 环境变量只按 allowlist 和变量名过滤，**不扫描值**；`minimal_env()` 的 docstring 明确承认，若秘密被塞进 `PATH` 的值仍可能泄露。
- 输出恰好等于上限不算截断；超过才算，见 `test_output_is_capped_and_exact_fit_is_not_truncated`（`backend/tests/test_runner.py:84`）。
- `truncated` 是 stdout/stderr 两线程共享的事件；一路超限会让运行循环停止并杀进程组，另一路读取也会停止。
- 这是 Linux/Unix 语义的进程组实现，不要把它写成跨平台容器隔离。

### 2.5 本地 FastAPI：token、Host、Origin 与回环绑定

**概念与实现**

位置：`backend/src/devsquad/server.py`。

- `serve()`（`:110`）只接收 port，固定 `host=BIND_HOST`，而 `BIND_HOST = "127.0.0.1"`（`:35`）；调用者不能传 `0.0.0.0`。
- FastAPI 实例在 `:40` 创建，docs、redoc、OpenAPI 都关闭；唯一业务路由是 `health()`（`:104`）。
- HTTP middleware `enforce_local_client()`（`:96`）让所有请求先经过 `authorize()`（`:79`）。
- `_host_allowed()`（`:43`）只接受 `127.0.0.1` 或 `localhost`，可带 1–65535 的端口。
- `_origin_allowed()`（`:54`）要求 `http`/`https`、本机 hostname，拒绝 userinfo、非根路径、query 和 fragment；没有 Origin 可以继续检查 token。
- `_token_ok()`（`:69`）要求配置值和请求值都非空，再用 `hmac.compare_digest` 比较 UTF-8 bytes。token 来自 `X-DevSquad-Token`。
- 非本机 Host/Origin 返回 403；缺失/错误 token 返回 401。错误响应不回显 token。

**为什么这样设计**

只绑定回环地址并不够：恶意网页可能请求本机端口，DNS rebinding 也可能让外部域名解析到 `127.0.0.1`。绑定地址、Host、Origin、共享 token 是不同防线。先检查 Host/Origin 再检查 token，使非本机请求即使知道 token 也被 403 拒绝。

**考虑过的备选**

- 只 bind `127.0.0.1`：文档明确认为不足。
- 信任 `X-Forwarded-Host`：不采用；模块 docstring `:17-18` 说明此进程不应放在代理后，forwarded headers 被忽略。
- 空 token 表示“关闭鉴权”：不采用；空 token fail closed。
- 自动开放交互式 API 文档：不采用，减少非必要路由。

**坑**

- 当前 Host allowlist 不包含 `::1`，不要声称支持 IPv6 回环。
- 自定义 token 不是登录系统；这里只是本机 sidecar 的共享秘密。
- 只有 `/health`，没有创建任务、读取 traces 或运行 Agent 的 HTTP API。

### 2.6 SQLite 任务表与追加式 traces

**概念与实现**

- `backend/src/devsquad/db.py:19` 的 `SCHEMA` 建两张表：`tasks` 和 `traces`。`traces` 有 `(task_id, seq)` 唯一约束及对应索引。
- `tasks` 前六列依次是 `id`、`repo_path`、`branch`、`worktree_path`、`status`、`created_at`，另有 `title` 和 `base_commit`。
- `connect()`（`db.py:59`）每次确保 schema 存在，设置 `sqlite3.Row`、autocommit、外键和 5000 ms busy timeout。外键 pragma 是每连接设置，不能只设一次。
- `TraceRecorder.record()`（`backend/src/devsquad/traces.py:37`）要求任务已存在，payload 必须是可 JSON 序列化的 dict 或 None；事件类型不限制为固定枚举。
- `record()` 用 `BEGIN IMMEDIATE` 开启写事务，查 `MAX(seq)+1`，插入，再 COMMIT；异常时 ROLLBACK。每个任务的序号从 1 开始。
- `list_events()`（`:98`）按 seq 返回并解码 JSON；不存在的任务返回空列表。
- `TraceEvent`（`:19`）是不可变 dataclass，返回给调用方时 payload 已解码。

**为什么这样设计**

traces 是未来 Agent 的追加式审计记录：模型轮次、工具调用、工具结果、限制触发等可以共用一张表。事件类型保持字符串，未来增加类型不需要数据库迁移。`BEGIN IMMEDIATE` 在计算下一个 seq 前取得写锁，配合唯一约束，避免两个写者给同一任务分配相同序号。

**考虑过的备选**

- 每连接依赖 SQLite 默认外键行为：不可行，SQLite 默认不开启外键，所以 `connect()` 每次显式开启。
- 在 Python 中查完 seq 后直接插入、不使用写事务：并发写者可能撞 `(task_id, seq)` 唯一约束，不采用。
- 固定事件类型 enum：本 PR 没采用，docstring 明确允许未来 loop 增加类型而不迁移。

**坑**

- `TraceRecorder` 目前只是存储层；`create_task()` 不自动写 trace，`run` 也不会产生 Agent 执行记录。
- `list_events()` 对未知任务返回 `[]`，而 `record()` 对未知任务抛 `ValueError`，两者语义不同。
- `CREATE TABLE IF NOT EXISTS` 不是完整迁移系统，已有数据库的列不会靠这段 schema 自动升级。
- cleanup 保留任务行并改状态，不是级联删除 traces。

### 2.7 测试、CI 与“没有模型调用”的约束

**概念与实现**

- `backend/pyproject.toml:37-43`：pytest 只找 `tests`，把项目根和 `src` 加入 `pythonpath`，默认参数 `-ra`，注册 `student` marker。
- `.github/workflows/ci.yml` 在 main push 和所有 PR 上运行：Ubuntu、Python 3.11、`pip install -e ".[dev]"`、`ruff check .`、`pytest`。
- `backend/tests/test_no_llm.py:17` 的 `test_package_source_has_no_shell_true_or_model_sdk` 扫描包源码，禁止 `shell=True`、常见模型 SDK import 和模型 API 域名。
- 未实现的学生测试同时带 `student` 和 `xfail(strict=False)`（当前只剩 BM25 与权限两份）。默认 `pytest` 中，未实现导致失败属于预期；`--runxfail` 才把它们当普通测试执行。

**为什么这样设计**

先把安全边界和行为规格写进测试，后续实现不能靠口头约定。非严格 xfail 让骨架 PR 可以合入 CI，同时给学生保留完整验收规格。

**备选与坑**

- 把未实现测试直接跳过：会失去断言语境；这里保留完整测试并用 xfail 标记。
- 只看默认 CI 绿色就宣称三份作业完成：错误。必须运行 `pytest -m student --runxfail`，让真实失败暴露出来。
- 非严格 xfail 在实现成功后可能显示 XPASS，但仍不强制 CI 失败；因此实现后也要主动用 `--runxfail` 验收。
- 源码正则扫描是 tripwire，不等同于证明所有间接网络行为都不可能发生。

## 3. 一条完整数据流：`run` → worktree → `cleanup`

### 3.1 创建阶段（按真实调用顺序）

执行：

```bash
devsquad run --repo /path/to/repo --task "给 parser 补上空输入测试"
```

调用链如下：

1. 安装后的 console script 根据 `backend/pyproject.toml:20` 进入 `devsquad.cli:main`；`main()` 调用 Typer `app()`。
2. Typer 分发到 `run_command()`（`backend/src/devsquad/cli.py:101`）。
3. `run_command()` 先读取 `workspaces_dir()` 和 `db_path()`（`backend/src/devsquad/config.py:21`、`:13`），再调用 `create_task(repo=repo, title=task, ...)`。
4. `create_task()`（`backend/src/devsquad/tasks.py:69`）清洗并校验标题。
5. `_require_git_repo()`（`:264`）依次执行 Git 检查：是否位于工作树、仓库顶层在哪里、HEAD 是否存在；再回到 `create_task()` 读取 `base_commit`。
6. `new_task_id()`（`:64`）生成 ID；代码派生分支名和 worktree 路径。
7. `_assert_outside_repo()`（`:278`）确认解析后的 worktree 不与目标仓库相同或互相包含，并确认目标路径尚不存在。
8. `git()`（`:236`）以 argv 执行 `git worktree add -b devsquad/<id> <worktree> HEAD`。Git 创建分支、目录和 linked `.git` 文件；原检出的 HEAD、当前分支、已有文件保持不变。
9. `create_task()` 创建 `Task` 对象，状态为 `created`。
10. `TaskStore.insert()`（`:179`）调用 `connect()`（`backend/src/devsquad/db.py:59`）；`connect()` 先经 `init_db()`（`:46`）建表，再插入任务行。
11. `run_command()` 用 `_echo_task()`（`backend/src/devsquad/cli.py:50`）打印 task ID、分支、worktree、仓库、状态、标题和 `agent_execution: deferred`，最后提示 Agent 后续 PR 才执行。

如果第 10 步失败，`_rollback_worktree()` 尝试删除第 8 步创建的 worktree 和分支。

简化图：

```text
console script / python -m devsquad
  → cli.main → Typer app → run_command
  → config.workspaces_dir + config.db_path
  → tasks.create_task
      → _require_git_repo → git rev-parse
      → new_task_id
      → _assert_outside_repo
      → git worktree add -b ... HEAD
      → TaskStore.insert → db.connect → init_db → INSERT tasks
  → _echo_task（不调用 Agent / CommandRunner / FastAPI）
```

`devsquad task list` 是旁路查询：`task_list()` → `TaskStore(db_path()).list_tasks()`（`tasks.py:213`）→ `format_task_line()`（`:167`），不会创建或删除 Git 资源。

### 3.2 清理阶段（另一个显式命令，不是 `run` 的自动收尾）

执行：

```bash
devsquad task cleanup <task-id>          # 安全模式
devsquad task cleanup <task-id> --force  # 明确丢弃任务改动
```

调用顺序：

1. Typer 进入 `task_cleanup()`（`backend/src/devsquad/cli.py:72`），调用 `cleanup_task(task_id=..., db_path=..., force=...)`。
2. `cleanup_task()`（`backend/src/devsquad/tasks.py:124`）通过 `TaskStore.get()`（`:203`）读任务；未知任务或已经 `cleaned` 的任务直接报错。
3. `_assert_removable()`（`:288`）无论 force 与否都检查分支名、任务 ID 对应关系和路径边界。
4. 非 force 时：`_is_dirty()`（`:301`）检查未提交和未跟踪文件；`_branch_exists()`（`:306`）与 `_is_merged()`（`:312`）检查任务提交是否已进入 HEAD/main/master。任一风险存在就抛 `TaskError`，CLI 退出 1，不删资源。
5. 安全检查通过或明确 force 后，`_remove_worktree()`（`:330`）执行 `git worktree remove [--force]`；目录已不存在则 `git worktree prune`。
6. 分支仍存在时，执行 `git branch -d`；force 时用 `git branch -D`。
7. `TaskStore.mark_cleaned()`（`:223`）把同一行更新为 `status='cleaned'`。
8. 再调用 `TaskStore.get()` 读回更新后的任务并返回；CLI 打印清理的 ID、worktree、分支和最终状态。

**这条数据流里没有 Agent。** `PathGuard`、`CommandRunner` 是为后续 Agent 工具准备的安全组件；当前 `create_task()` 使用自己的仓库路径检查，并不会顺手调用 `CommandRunner`，`TraceRecorder` 也不会自动记录上述步骤。

## 4. 三份学生作业：BM25、权限策略与主循环

### 4.0 测试环境与建议顺序

从仓库根目录执行（与 README 和 pyproject 一致）：

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

常用命令：

```bash
pytest                              # 已实现功能；学生测试按 xfail 处理
pytest -m student --runxfail         # 未实现作业按真实通过/失败执行
ruff check .                        # 静态检查
```

BM25 已由 PR #4 实现，`run_loop` 也已实现，当前只剩 `PermissionPolicy.check()` 一份作业。阅读顺序仍建议 **`bm25.py` → `permissions.py` → `agent/loop.py`**：

1. BM25 是纯内存算法，先熟悉“docstring + 数值测试就是合同”。
2. 权限策略复用已经完成的 `PathGuard`，建立 `Decision` 的语义，这是当前唯一待实现的作业。
3. 主循环已把步数、费用、消息 transcript、权限和工具分发组合起来；它的测试使用 fake model 和 fake permissions，不依赖真实模型，也不要求 BM25。

完成权限作业后先跑对应文件命令；最后跑完整的 `pytest -m student --runxfail`、普通 `pytest` 和 `ruff check .`。

### 4.1 `backend/src/devsquad/retrieval/bm25.py`

**BM25 已由 PR #4 实现。** 老板现在的任务不是补写 `add()` 或 `search()`，而是读懂实现，并能回答 `backend/tests/test_bm25.py` 的相关问题。

阅读重点：

1. `tokenize()` 如何分词；`add()` 如何维护词频、文档长度和文档频率，以及重复 `doc_id` 为何不会留下半批数据。
2. `search()` 如何使用默认 `k1=1.5`、`b=0.75` 和非负 IDF 公式打分，如何处理重复查询词、排序及 `top_k`。
3. 对照测试解释具体分数、短文档优先、同分按 `doc_id` 排序、`b=0` 的效果，以及空输入和非法参数的行为。

运行验收：

```bash
pytest -m student --runxfail tests/test_bm25.py
```

### 4.2 `backend/src/devsquad/tools/permissions.py`

**必须保持的公开接口**（`permissions.py:23-48`）：

```python
READ_TOOLS = frozenset({"read_file", "list_dir", "grep"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
COMMAND_TOOLS = frozenset({"run_command"})
NETWORK_TOOLS = frozenset({"http_get", "http_post"})

class Decision(StrEnum):
    ALLOW = "allow"
    ASK_HUMAN = "ask_human"
    DENY = "deny"

class PermissionPolicy:
    def __init__(self, worktree: Path, deny_tools: set[str] | None = None) -> None: ...
    def check(self, tool_name: str, arguments: dict) -> Decision: ...
```

待实现的是 `PermissionPolicy.check()`（`:48`）。`Decision` 已存在，不要另造一套字符串。

**决策表**

1. `deny_tools` 优先级最高；网络工具和未知工具都 `DENY`，即 fail closed。
2. 读工具：
   - `list_dir` 没有 path 时，root 默认 `ALLOW`。
   - 其他情况需要 path，并通过 `PathGuard.check_read()`；成功 `ALLOW`，越界或命中密钥 `DENY`。
   - `read_file`/`grep` 缺 path 为 `DENY`。
3. 写工具：
   - 缺 path、路径越界、写 `.git`、原始或解析后路径命中密钥，全部 `DENY`。
   - 安全的 `write_file`/`edit_file` 也只能 `ASK_HUMAN`，不能直接 `ALLOW`。
   - `PathGuard.check_write()` 不查密钥，所以这里必须结合 `is_secret_filename()` 检查原始路径和 resolved 路径的名称。
4. `run_command`：无 cwd 时默认 root，`ASK_HUMAN`；有 cwd 时必须通过 `check_read()` 且为目录，否则 `DENY`。本 PR 不要求解析 argv 里的文件名。
5. 复用 `PathGuard`，不要再写 `startswith` 或自己的符号链接逻辑。

**对应测试**：`backend/tests/test_permissions.py`，文件级 marker 在 `:17-23`。

| 测试名 | 验证点 |
| --- | --- |
| `test_read_inside_is_allowed` | 普通读取允许。 |
| `test_grep_inside_is_allowed` | grep 使用同一读取规则。 |
| `test_list_dir_without_path_is_the_worktree_root` | 缺省 path 的特例。 |
| `test_list_dir_outside_is_denied` | list_dir 越界拒绝。 |
| `test_write_and_edit_inside_ask_the_human` | 安全写入仍需人工确认。 |
| `test_run_command_asks_the_human` | 命令默认人工确认。 |
| `test_read_traversal_outside_the_worktree_is_denied` | 相对与绝对越界。 |
| `test_read_symlink_escape_is_denied` | 符号链接逃逸。 |
| `test_write_outside_is_denied_not_ask` | 越界写入是 deny，不是 ask。 |
| `test_write_into_git_is_denied` | 根及嵌套 `.git` 都拒绝。 |
| `test_secret_read_and_write_are_denied` | 密钥读写拒绝，模板读取允许。 |
| `test_run_command_cwd_escape_is_denied` | 命令 cwd 越界。 |
| `test_network_and_unknown_tools_are_denied` | 网络与未知工具 fail closed。 |
| `test_deny_list_overrides_an_otherwise_safe_read` | deny list 覆盖其他规则。 |

运行：

```bash
pytest -m student --runxfail tests/test_permissions.py
```

### 4.3 `backend/src/devsquad/agent/loop.py`

**必须保持的公开接口**（`loop.py:28-72`）：

```python
@dataclass(frozen=True)
class ModelTurn:
    cost_usd: float
    final_text: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None

@dataclass(frozen=True)
class LoopResult:
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
) -> LoopResult: ...
```

`run_loop()`（`:69`）已实现：关闭 `planner_enabled` 时直接进入共享编码循环，不调用规划模型、不注入计划、也不记录 `run.plan`；开启时才先规划、注入计划并记录 `run.plan`。model 是 duck typing：测试替身提供 `peek_cost(messages)` 和 `complete(messages)`；permissions 提供 `check(tool_name, arguments)`。不要 import LLM SDK，也不要联网。

**每轮顺序就是验收点**

1. 在第一次模型调用前校验：`max_steps < 1` 或 `max_cost_usd < 0` 抛 `ValueError`。
2. 初始消息只能是 `[{"role": "user", "content": task}]`。
3. 每轮先检查 `steps >= max_steps`：若是，返回 `step_limit`，不再 `peek_cost`，也不再 `complete`。
4. 调 `float(model.peek_cost(messages))` 预估下一次费用；若 `spent + upcoming > max_cost_usd`，返回 `cost_limit`，不能执行这次 `complete`。等于预算允许，所以是 `>` 而非 `>=`。
5. 调 `model.complete(messages)` 后才增加一步并累加实际 `turn.cost_usd`。steps 统计真正完成的模型轮次，不统计 peek 或工具次数。
6. 没有 `tool_name`：追加 assistant 最终消息；有 `final_text` 则 `completed`，两者都为 None 则 `error`。
7. 有 `tool_name`：先追加 assistant 工具请求消息；工具名不在 `tools` 中直接返回 `error`，否则调用 `permissions.check(name, dict(turn.tool_args or {}))`。
   - `DENY`：不执行工具，返回 `denied`。
   - `ASK_HUMAN`：不执行工具，返回 `ask_human`。
   - `ALLOW`：通过 `dispatch_tool(name, args, tools, repo_root)` 执行工具，把结果追加为 tool 消息，再进入下一轮。
   - 其他 decision：返回 `error`。
8. 工具执行按沙箱边界处理：`SandboxError` 返回 `denied`，其他异常记录 `tool.error` 后返回 `error`，异常原文不进入 transcript 或 trace。
9. 提供 `trace_recorder` 和 `task_id` 时记录 `loop.result` 与工具拒绝/错误事件；关闭规划时不记录 `run.plan`。

消息格式不能自行改名：

```python
{"role": "user", "content": task}
{"role": "assistant", "content": "", "tool_name": name, "tool_args": args}
{"role": "tool", "name": name, "content": result_if_str_else_str_result}
{"role": "assistant", "content": final_text}
```

可能状态为 `completed`、`step_limit`、`cost_limit`、`denied`、`ask_human`、`error`。denied/ask-human 时，assistant 的工具请求已经在 transcript 中，但工具结果不应存在。

**对应测试**：`backend/tests/test_agent_loop.py`（已移除学生 xfail marker）；fake model `ScriptedModel` 在 `:16`，fake permissions `FakePermissions` 在 `:34`。

| 测试名 | 验证点 |
| --- | --- |
| `test_final_answer_without_tools` | 无工具完成、步数费用和消息。 |
| `test_step_limit_stops_before_another_model_call` | 到步数后不再 peek/complete。 |
| `test_cost_limit_does_not_run_the_call_that_would_exceed_the_budget` | 超预算的调用不能执行。 |
| `test_exact_budget_is_allowed` | 恰好到预算允许。 |
| `test_first_call_over_budget_does_not_call_the_model` | 第一次就超预算时 complete 次数为 0。 |
| `test_allow_dispatches_tool_and_feeds_the_result_back` | 工具分发、结果回填和下一轮输入。 |
| `test_deny_does_not_execute_the_tool` | deny 不执行且保留请求消息。 |
| `test_ask_human_does_not_execute_the_tool` | ask-human 不执行。 |
| `test_unknown_tool_is_an_error_when_allowed` | 权限允许但实现缺失时 error。 |
| `test_invalid_limits_raise_value_error` | 非法限制在模型调用前报错。 |

运行：

```bash
pytest tests/test_agent_loop.py
```

## 5. 已知遗留项与边界

以下不是“笔记漏看”，而是源码、README 或模块 TODO 明确留下的边界：

1. **Agent 主循环已实现，但尚未接入 `devsquad run`。** `run` 只创建工作区；`run_loop` 已含默认编码循环和可选 Planner，但完整角色编排、LangGraph 和真实模型调用仍是后续工作。
2. **权限作业仍是 `NotImplementedError`。** BM25 已由 PR #4 实现，`run_loop` 也已实现；默认 pytest 的绿色包含非严格 xfail，不代表 `PermissionPolicy.check` 已经可用。
3. **没有网络隔离。** `runner.py:15-19` 是明确 TODO；代理环境变量不算隔离。未来需要用 OS 能力，并验证真实 `connect()` 失败。
4. **没有完整的子进程文件系统隔离。** `PathGuard` 管应用检查过的路径与 cwd，管不住子进程自己打开绝对路径；`shell=False` 也不改变这一点。
5. **路径检查有 TOCTOU 窗口。** resolve 与后续 open 之间路径可能被替换；当前不是内核级沙箱。
6. **trace 已接入主循环。** 提供记录器和任务 ID 时，主循环记录 `loop.result` 与工具拒绝/错误事件，开启规划时记录 `run.plan`；`devsquad run` 仍未调用 `run_loop`，因此这些事件不会由 CLI 自动产生。
7. **本地服务只有健康检查。** 没有任务/工具/模型 HTTP API，也没有 CLI 启动命令。
8. **前端、Tauri、evals 都是占位。** 没有页面、桌面壳、基准数据集或可报告的通过率。
9. **费用仍用 float USD。** `ModelTurn` docstring（`loop.py:33-37`）提醒二进制浮点问题，未来可改整数 micro-dollars；本 PR 明确使用 `>` 比较预算。
10. **`.env` 不自动加载。** 这是当前明确设计，不要运行后把“token 为空导致 401”误判成鉴权代码失效。
11. **学生模块尚未组成完整产品链路。** BM25 没被 Agent 调用，权限策略和主循环也未接入 `devsquad run`；即使三份作业完成，也还要后续集成工作。

老板合并前的合理检查顺序是：先看 diff，再跑普通 `pytest` 和 `ruff check .` 验证骨架，再确认自己理解学生测试为何暂时 xfail。合并后按第 4 节顺序亲手实现，不要只追求默认 CI 绿色。

## 6. 五道面试题与参考答案

### 题 1：为什么用 git worktree，而不是在原仓库切分支或完整 clone？`cleanup --force` 是否意味着可以删任何内容？

**参考答案：**

原仓库可能有未提交修改，切分支会改变用户当前工作区，也不利于多任务并行。完整 clone 会复制更多对象、增加成本；worktree 共享对象库，同时给每个任务独立目录和 `devsquad/<id>` 分支。本 PR 在 `create_task()` 中从 HEAD 创建 worktree，并用 `_assert_outside_repo()` 保证目录在仓库外。

`--force` 只跳过脏工作区和未合并提交保护。`_assert_removable()`（`tasks.py:288`）始终验证分支必须匹配 `devsquad/<task-id>`，并拒绝目标仓库本身、其内部或其祖先路径。因此 force 不等于无保护删除。对应测试是 `test_cleanup_refuses_to_delete_the_main_checkout`。

### 题 2：为什么安全路径判断必须同时看原始路径和 `resolve()` 后的路径？只用其中一个会漏什么？

**参考答案：**

resolve 后的路径用于真实边界检查，能发现 `..` 和符号链接逃逸；`relative_to(root)` 又避免 `/tmp/wt` 与 `/tmp/wt-evil` 的字符串前缀误判。但敏感文件名可能在 resolve 前后任一側出现：

- `.env -> ordinary.txt`：只看 resolved 会漏掉原始敏感名字。
- `notes.txt -> id_rsa`：只看 raw 会漏掉真实敏感目标。

所以 `PathGuard._touches_secret()` 同时检查两侧名称，`_touches_git()` 也采用同样思路。即便如此，它仍有检查到使用之间的 TOCTOU 窗口，且无法约束子进程任意 `open()`，不能称为完整沙箱。代码见 `pathguard.py:129-163`。

### 题 3：`shell=False` 已经防止命令注入了，为什么还需要最小环境、进程组和输出线程？做到这些后是否是沙箱？

**参考答案：**

它们解决的是不同问题。argv + 固定 `shell=False` 避免 shell 把 `&&` 等文本解释成命令；最小环境避免子进程继承 API key/token；新进程组让超时可以杀掉子进程及后代；stdout/stderr 两个读取线程避免管道阻塞，并用字节上限限制内存占用。

这些仍不是沙箱：被执行程序可以直接联网、读取仓库外文件，环境变量的值也未做秘密扫描。本 PR 在 `runner.py:15-23` 明确留下网络和文件系统隔离缺口，不能用 `HTTP_PROXY` 冒充禁网。

### 题 4：本地服务已经只绑定 `127.0.0.1`，为什么还检查 Host、Origin 和 token？为什么空 token 不能表示关闭鉴权？

**参考答案：**

回环绑定只限制监听地址，恶意网页仍可能尝试访问本机端口，DNS rebinding 也可能把外部域名指到回环地址。Host 检查防止把任意域名当成本机服务，Origin 检查限制网页来源，自定义 token 再给本机调用方一个共享秘密。本 PR 的 `authorize()` 先查 Host/Origin，再查 token；前者失败为 403，后者失败为 401。

空 token 若表示关闭鉴权，漏配环境变量就会 silently 变成无鉴权服务；当前 `_token_ok()` 让空配置 fail closed。`hmac.compare_digest` 用于秘密比较，响应也不回显 token。见 `server.py:43-93`。

### 题 5：Agent 的费用上限为什么必须在 `complete()` 前用 `peek_cost()` 检查，而且比较是 `>` 不是 `>=`？步数上限又为什么放在每轮最前面？

**参考答案：**

费用检查的目的是阻止“会使总费用超过预算”的模型调用实际发生，所以必须先 peek，再 complete；如果先 complete，钱已经花了才停止就没有意义。规格允许总费用恰好等于预算，因此只有 `spent + upcoming > max_cost_usd` 才停止，等号应继续。

每轮先检查 `steps >= max_steps`，可保证达到上限后连下一次 `peek_cost()` 都不做，更不会多调用一次模型。steps 只统计实际完成的模型轮次。对应测试 `test_cost_limit_does_not_run_the_call_that_would_exceed_the_budget`、`test_exact_budget_is_allowed`、`test_first_call_over_budget_does_not_call_the_model` 和 `test_step_limit_stops_before_another_model_call`，规格集中在 `agent/loop.py:75-103`。
