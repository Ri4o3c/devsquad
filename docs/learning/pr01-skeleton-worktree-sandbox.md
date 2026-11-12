# PR01：骨架、worktree、沙箱

这是一份短笔记，方便对照代码复习。更细的讲解可以之后再补。

## 这次做了什么

- 仓库分成 `backend/`、`frontend/`（占位）、`docs/`、`evals/`（占位）。
- 创建任务的命令只有 `devsquad run --repo <path> --task "<requirement>"`。它在目标仓库**外面**建分支 `devsquad/<id>` 和 git worktree，并把任务写入 SQLite。智能体这次不跑。
- 辅助命令是 `devsquad task list` 和 `devsquad task cleanup <id>`。没有 `task new`。
- 路径守卫和命令运行器是实现了的。Agent 主循环、工具权限、BM25 只有接口和会失败的测试。
- FastAPI 只有本机 `/health`。trace 表可以记事件，但没有模型调用。

## 为什么用 worktree

任务要改代码，又不能动用户当前打开的目录。`git worktree add` 在另一个目录检出一条新分支，原来的 `HEAD`、当前分支和未提交文件都留在原地。

Worktree 必须放在仓库外面（`DEVSQUAD_WORKSPACES`，默认 `~/.devsquad/workspaces`）。放进仓库里会被拒绝。linked worktree 的 `.git` 是一个文件，指向主仓库的 git 目录，所以不能让任务往 `.git` 里写。

完整取舍写在 `docs/adr/0001-worktree-isolation.md`：换分支会撞上用户的未提交文件；整仓 clone 慢；只复制文件就没有可审查的提交。容器留到以后做进程隔离，不拿来冒充这次的版本隔离。

## 为什么命令用参数列表

`subprocess` 的 `shell=True` 会把字符串交给 shell。`"pytest && rm -rf ..."` 这种文本会被拆成两条命令。参数列表配合写死的 `shell=False` 时，`&&` 只是下一个参数的字符，不会被执行。

这还不是沙箱。子进程仍然可以自己打开任意文件，也可以自己连网。网络隔离这次只写成 TODO，没有用 `HTTP_PROXY` 假装已经封网。超时则杀掉整个进程组，而不只是直接子进程。

## 路径穿越和符号链接

判断「在不在 worktree 里」要先 `resolve()`（展开 `..`、跟着符号链接走），再用 `Path.relative_to`。下面两种写法都会错：

- `str(path).startswith(str(worktree))`：`/tmp/wt-evil` 以 `/tmp/wt` 开头，但不是它的子目录。
- 只看链接的名字：worktree 里一个叫 `notes.txt` 的链接可以指向外面的文件；一个叫 `.env` 的链接也可以指向普通文件。名字和最终路径都要看。

检查发生在真正打开文件之前，中间有一段时间窗口。守卫也挡不住子进程里的 `open("/etc/passwd")`。

## 关键位置

| 做什么 | 位置 |
| --- | --- |
| `run --repo --task` | `backend/src/devsquad/cli.py:101` |
| 创建 worktree 并写 SQLite | `backend/src/devsquad/tasks.py:69` |
| 拒绝把 worktree 放进目标仓库 | `backend/src/devsquad/tasks.py:278` |
| 清理；`--force` 也不能删主检出 | `backend/src/devsquad/tasks.py:124` 和 `:288` |
| 任务表 / trace 表 | `backend/src/devsquad/db.py:19` |
| 解析路径并确认还在 worktree 内 | `backend/src/devsquad/sandbox/pathguard.py:129` |
| 拒绝读密钥文件名 | `backend/src/devsquad/sandbox/pathguard.py:65` |
| 拒绝写 `.git` | `backend/src/devsquad/sandbox/pathguard.py:105` |
| 参数列表、`shell=False` | `backend/src/devsquad/sandbox/runner.py:135` 和 `:164` |
| 最小环境变量 | `backend/src/devsquad/sandbox/runner.py:99` |
| 网络隔离 TODO | `backend/src/devsquad/sandbox/runner.py:15` |
| 只绑定 `127.0.0.1` | `backend/src/devsquad/server.py:35` 和 `:110` |
| Host / Origin / token | `backend/src/devsquad/server.py:79` |
| trace 记录 | `backend/src/devsquad/traces.py:37` |
| 三份学生作业的 `NotImplementedError` | `backend/src/devsquad/agent/loop.py:115`、`backend/src/devsquad/tools/permissions.py:88`、`backend/src/devsquad/retrieval/bm25.py:81` |

## 五个面试题

**1. 为什么不在用户当前的工作区里 `git checkout -b`？**

会换掉用户正在看的文件，未提交的修改也可能被卷进去，而且两个任务不能同时各改一份。worktree 让原目录保持不动，任务自己有一条 `devsquad/<id>` 分支。

**2. 用 `path.startswith(root)` 判断路径是否越界，错在哪？**

字符串前缀分不清 `/tmp/wt` 和 `/tmp/wt-evil`。要先解析符号链接和 `..`，再对两个真实路径做 `relative_to`。

**3. `shell=False` 之后，命令运行器还缺什么？**

它避免了 shell 把 `&&` 解释成第二条命令，也限制了 cwd、环境变量、超时和输出大小。它没有禁止子进程访问 worktree 以外的文件，也没有禁止联网。后者在 runner 里标成 TODO，不能靠设置代理变量冒充。

**4. worktree 里面的符号链接为什么仍然危险？**

`resolve()` 会跟着链接走。链接可以指向仓库外，也可以指向 `.git` 或 `id_rsa`。所以要同时看调用者写下的路径，和解析之后的真实路径。

**5. 服务已经绑在 127.0.0.1 上，为什么还要检查 Host 和 token？**

本机端口仍然可能被网页用跨站请求碰到，DNS rebinding 也可以把一个公网名字指到 127.0.0.1。Host / Origin 拒绝非本机名字，`X-DevSquad-Token` 用 `hmac.compare_digest` 比较。令牌为空时直接拒绝。
