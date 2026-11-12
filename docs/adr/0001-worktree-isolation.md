# ADR 0001：每个任务用仓库外的 git worktree

- 状态：已接受（阶段 1）
- 日期：2026-11-12

## 背景

DevSquad 要在用户的仓库上做改动，同时不能动用户当前正在看的工作区。一个任务一份目录，后面才有可能并行跑多个任务，也才有可能在失败时把改动整份丢掉。

用户的仓库里往往还有未提交的文件。如果直接在那个目录里 `git checkout` 一条新分支，这些文件会被换走，或者和任务的改动混在一起。

## 决定

`devsquad run --repo <path> --task "<requirement>"` 做三件事：

1. 在目标仓库里创建分支 `devsquad/<task-id>`，指向当时的 `HEAD`。
2. 用 `git worktree add` 把这条分支检出到仓库**外面**的目录，默认是 `~/.devsquad/workspaces/<task-id>`，也可用 `DEVSQUAD_WORKSPACES` 改。如果这个目录解析之后落在目标仓库里面，命令直接拒绝。
3. 在 SQLite `tasks` 表记下 `id`、`repo_path`、`branch`、`worktree_path`、`status`、`created_at`（另外还有标题和创建时的 commit，方便清理和展示）。

目标仓库的当前分支和已有文件保持不动。linked worktree 里的 `.git` 是一个文件，内容类似 `gitdir: <主仓库>/.git/worktrees/<id>`，不是一个新的独立仓库。

`devsquad task cleanup <id>` 同时删掉 worktree 和分支。工作区有未提交改动，或者分支上的提交还不在 `HEAD` / `main` / `master` 里时，拒绝删除，除非 `--force`。`--force` 仍然不能删除目标仓库自己的检出，也不能删除名字不是 `devsquad/<id>` 的分支。

没有单独的 `task new`。创建任务的公开入口只有 `run`。

## 后果

- 用户当前的工作区可以继续编辑。任务的 diff 就是这条分支相对出发点的提交。
- 多个任务可以同时各有一个目录。
- 清理必须成对：只删目录不删分支，或者只删分支不删 worktree 注册信息，都会留下垃圾。实现走 `git worktree remove` 再 `git branch -d/-D`。
- 路径守卫要特别拒绝写入 `.git`。在 linked worktree 里写这个文件，改的是主仓库的 git 元数据。
- 控制面调用 `git` 用的是参数列表。这和后面给智能体用的命令运行器不是同一层：控制面是可信代码，运行器才是限制 cwd、环境和超时的地方。

## 考虑过的其他做法

| 做法 | 不选的原因 |
| --- | --- |
| 在用户当前的检出里换分支 | 会带走或挡住用户还没提交的文件，也不能两个任务并行 |
| 每个任务完整 `git clone` | 慢，对象库复制一份，和原仓库的关系更远 |
| 只复制文件、不走 git | 没有可审查的提交，清理和回滚都要自己发明 |
| 每个任务一个容器 | 能加强隔离，但解决的是进程沙箱，不是「改动落在哪条分支上」。网络和文件系统隔离留到后面的 PR，不在这次用容器假装已经做到 |

## 相关代码

- `backend/src/devsquad/tasks.py`：创建、清理、拒绝危险路径
- `backend/src/devsquad/cli.py`：`run` / `task list` / `task cleanup`
- `backend/src/devsquad/sandbox/pathguard.py`：解析符号链接，拒绝逃出 worktree，拒绝写 `.git`
