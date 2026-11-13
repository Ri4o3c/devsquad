# PR4–5：BM25 实现与 Windows 沙箱修复

老板，这份笔记对照仓库中的实现与测试整理，重点是能解释**公式、边界条件和修复动机**，而不只是背 API。

- PR4：`student/bm25` 分支引用指向 `26e1dc94dd1dfe9b51c06d2f7537bfcaadf1d085`。
- PR5：Windows 修复提交为 `a7f5b4102d8dcba33ecd93a7e4cc860669db41db`（短哈希 `a7f5b41`）。

## 一、PR4：BM25 实现

核心文件：

- `backend/src/devsquad/retrieval/bm25.py`
- `backend/tests/test_bm25.py`

### 1. 分词与索引数据

`tokenize()` 先执行 `text.lower()`，再用正则 `[a-z0-9]+` 提取连续片段（`bm25.py:54-60`）：

```python
_TOKEN_RE = re.compile(r"[a-z0-9]+")
return _TOKEN_RE.findall(text.lower())
```

因此 `Foo_Bar` 变成 `["foo", "bar"]`。下划线是分隔符；这份实现**没有驼峰拆词**，索引和查询则复用同一个 `tokenize()`。正则只认 ASCII 的 `[a-z0-9]`，纯中文文本不会切出任何词。

`BM25Index.__init__()` 保存四类统计量（`bm25.py:75-90`）：

- `_tf`：`doc_id -> Counter`，记录每篇文档内各词的词频。
- `_doc_len`：`doc_id -> dl`，记录分词后的文档长度。
- `_df`：`term -> df`，记录多少篇文档包含该词。
- `_total_len`：所有文档的 token 总数，用来计算 `avgdl`。

`Chunk.path` 只是元数据，不参与打分。默认参数是 `k1=1.5`、`b=0.75`；`k1 < 0`、`b` 不在 `[0, 1]` 内会抛 `ValueError`。

### 2. `add()` 为什么有两遍循环

`BM25Index.add()` 在 `bm25.py:92-110`：

1. **第一遍只校验**：用 `seen` 检查本批内部重复，同时检查 `doc_id` 是否已存在于 `_tf`。
2. **第二遍才写入**：分词并生成 `Counter`，依次更新 `_tf`、`_doc_len`、`_total_len` 和 `_df`。

这样，只要重复 ID 触发 `ValueError`，就不会留下“本批前几条已写入、后几条失败”的半成品状态。它保证的是代码明确处理的**重复 ID 失败场景**，不要把它泛化成对任意异常都成立的通用数据库事务。

写入时有一个重要细节：

```python
self._df.update(tf.keys())
```

这里更新的是每篇文档的**去重词表**，所以同一个词在某篇文档出现多次，`df` 仍只增加 1；`tf` 才记录出现次数。测试 `test_duplicate_doc_id_raises_and_does_not_keep_a_partial_batch()` 和 `test_duplicate_inside_one_batch_raises()` 分别覆盖跨批、批内重复。

### 3. 打分公式与四个关键点

`BM25Index._idf()` 和 `search()` 使用：

```text
idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
K(D)   = k1 * (1 - b + b * dl / avgdl)
贡献    = idf(t) * tf * (k1 + 1) / (tf + K(D))
```

#### IDF 为什么恒正

对索引中实际存在的词，`1 <= df <= N`，所以：

- `N - df + 0.5 >= 0.5`；
- `df + 0.5 > 0`；
- 对数里的 `1 + 正数 > 1`，故 `ln(...) > 0`。

即使一个词出现在所有文档中，它的 IDF 也只是较小的正数，不会变成负数。索引中不存在的查询词在 `search()` 里直接 `continue`，贡献为 0（`bm25.py:133-136`）。

#### `k1`：词频饱和

固定文档长度时，把 `K(D)` 看作常量，词频因子为：

```text
tf * (k1 + 1) / (tf + K(D))
```

它随 `tf` 增加而增加，但边际增长变小，并趋近上限 `k1 + 1`。因此重复堆词不会获得线性增长的分数。`k1` 越小通常越早饱和；越大则越接近线性词频作用。

#### `b`：长度归一化

`dl / avgdl` 衡量文档相对平均长度。`b > 0` 时，较长文档的 `K(D)` 和分母更大；在 `tf`、`idf` 相同的情况下，分数会被压低。

- `b=0`：长度项消失，不做长度归一化。
- `b=1`：使用完整的相对长度归一化。
- 中间值：两者折中。

这不是“长文档质量差”，而是校正长文档天然拥有更多匹配机会的影响。

#### `avgdl == 0` 与其他边界

`search()` 在计算 `avgdl = _total_len / n` 之前先检查（`bm25.py:120-129`）：

- `top_k < 1`：抛 `ValueError`。
- 查询无 token、索引为空或 `_total_len == 0`：返回 `[]`。

所以所有文档都切不出词时不会发生除零。查询词通过 `dict.fromkeys()` 去重并保留首次出现顺序，`"bird bird"` 与 `"bird"` 的结果相同。

当前实现是每个查询词遍历 `_tf.items()`，不是 posting-list 倒排索引实现。最后只保留 `score > 0` 的结果，排序键为（`bm25.py:147-150`）：

```python
hits.sort(key=lambda item: (-item[1], item[0]))
return hits[:top_k]
```

即**分数降序，同分按 `doc_id` 字符串升序**，再截取前 `top_k` 条，保证结果可复现。

### 4. 为什么查询 `cat` 时 d2 高于 d1

测试 `_index()` 在 `test_bm25.py:43-53` 建立：

| 文档 | 文本 | `dl` | `tf(cat)` |
| --- | --- | ---: | ---: |
| d1 | `cat dog dog` | 3 | 1 |
| d2 | `cat bird` | 2 | 1 |
| d3 | `bird bird bird` | 3 | 0 |

所以 `N=3`、`df(cat)=2`、`avgdl=(3+2+3)/3=8/3`，并且：

```text
idf(cat) = ln(1 + (3 - 2 + 0.5) / (2 + 0.5))
         = ln(1.6)
         ≈ 0.4700036292
```

取 `k1=1.5`、`b=0.75`：

| 文档 | `dl / avgdl` | 长度项 `1-b+b*dl/avgdl` | `K(D)` | `cat` 得分 |
| --- | ---: | ---: | ---: | ---: |
| d1 | `9/8` | `1.09375` | `1.640625` | `0.4449738502` |
| d2 | `3/4` | `0.8125` | `1.21875` | `0.5295815541` |

两篇文档的 `tf(cat)` 和 IDF 都相同；d2 更短，长度项和分母更小，所以得分更高。d3 不含 `cat`，不会进入结果。对应测试是 `test_search_cat_prefers_the_shorter_document()`（`test_bm25.py:63-68`）。

对照实验：`b=0` 时，两篇文档的词频因子都等于 1，得分都等于 `idf(cat)`；此时按 `doc_id` 升序变成 **d1、d2**。见 `test_b_zero_removes_the_length_penalty()`。

### 5. PR4 面试题

#### 题 1：BM25 的 `k1` 和 `b` 分别解决什么问题？

**参考答案：** `k1` 控制词频饱和。词频因子随 `tf` 增长但趋近 `k1+1`，避免重复词线性刷分。`b` 控制相对文档长度归一化：`b=0` 关闭，`b=1` 完整启用。前者处理“同词重复多少次”，后者处理“文档相对平均长度有多长”。

#### 题 2：为什么 `add()` 要先完整校验再写入？`df` 为什么不能按词频累加？

**参考答案：** 先检查已有 ID 和批内 `seen`，重复时就能在任何状态修改前抛错，避免部分批次残留。`df` 是包含某词的文档数，同一文档出现多次仍只算一篇，所以代码对 `tf.keys()` 更新，而不是对 `tf.values()` 求和。

#### 题 3：d1、d2 的 `cat` 都出现一次，为什么 d2 排前面？怎样让 d1 排前面？

**参考答案：** 两者 IDF、TF 相同，但默认 `b=0.75` 时 d1 更长，分母中的长度归一化项更大，所以 d1 得分较低。d2、d1 的分数约为 `0.5295815541`、`0.4449738502`。设 `b=0` 后两者同分，排序键 `(-score, doc_id)` 使 d1 排在 d2 前；这不是相关性发生了变化，而是长度归一化被关闭并启用了确定性同分规则。

## 二、PR5：Windows 沙箱修复

核心文件：

- `backend/src/devsquad/sandbox/runner.py`
- `backend/src/devsquad/sandbox/pathguard.py`
- `backend/tests/fsutil.py`
- `backend/tests/test_pathguard.py`
- `backend/tests/test_runner.py`
- `backend/tests/conftest.py`
- `.github/workflows/ci.yml`
- `README.md`

### 1. Windows 为什么不能照搬 `os.killpg`

POSIX 路径在 `Popen` 时设置 `start_new_session=True`，让子进程拥有独立会话；停止时 `_stop_process_group()` 调用：

```python
os.killpg(proc.pid, signal.SIGKILL)
```

但 Windows 没有这套 POSIX `killpg` 接口，而且代码注释明确指出 Windows 会忽略 `start_new_session`（`runner.py:163-170`）。只调用 `Popen.kill()` 又只结束直接子进程，可能留下孙进程，超时也就失去了“整棵树都停止”的意义。

修复后的 Windows 路径分两步：

1. `CommandRunner.run()` 在 `os.name == "nt"` 时传入 `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`；POSIX 仍用 `start_new_session=True`。
2. `_stop_process_group()` 在 Windows 转交 `_stop_windows_tree()`（`runner.py:305-317`），后者执行：

```python
subprocess.run(
    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
    capture_output=True,
    check=False,
    creationflags=flags,
)
```

其中 `/F` 强制结束，`/T` 结束进程树，`/PID` 指定根 PID。`flags` 通过 `getattr(subprocess, "CREATE_NO_WINDOW", 0)` 取得，避免额外控制台窗口；命令使用参数列表，不经过 shell。随后仍调用一次 `proc.kill()` 兜底，并捕获 `OSError`。POSIX 路径则在 `ProcessLookupError` 时返回，在 `PermissionError` 时尝试 `proc.kill()`。

`taskkill /T` 按父 PID 遍历进程树，父进程已退出的孤儿孙进程可能漏掉；`CREATE_NEW_PROCESS_GROUP` 主要影响 Ctrl+C/Ctrl+Break，`taskkill` 并不依赖它。

`test_timeout_kills_a_sleeping_process()` 覆盖超时后及时返回且返回码非零；不要把它说成专门的 Windows 孙进程测试，它的测试体只是让一个 Python 进程睡眠。

### 2. 路径守卫的大小写问题为什么是安全漏洞

Windows 文件路径通常不区分大小写，`.git`、`.GIT`、`.Git` 可以指向同一 Git 元数据路径。如果守卫只做 `part == ".git"` 的精确比较，攻击者就能用 `.GIT/config` 绕过“禁止写 `.git`”的规则。对 linked worktree 来说，`.git` 还可能是指向主仓库 Git 目录的文件，因此这不只是路径显示问题，而可能让任务修改真实仓库元数据。

修复后的逻辑位于 `pathguard.py`：

- `_is_git_component()`（`:116-119`）：Windows 用 `part.casefold() == ".git"`；其他系统仍用精确比较。
- `_touches_git()`（`:211-215`）：PR5 前就会同时检查解析后的路径和调用者原始路径；PR5 保留这套双重检查，改用 `_relative_parts()` 拆分路径，并用 `_is_git_component()` 替代 `".git" in parts` 的精确比较。
- `_touches_secret()`（`:206-209`）：也改用 `_relative_parts()` 获取解析后路径部分；绝对原始路径则由 `_user_parts()` 经同一辅助函数处理。
- `_for_compare()`（`:82-86`）：先经 `_strip_extended_prefix()` 去掉 Windows 的 `\\?\` 或 `\\?\UNC\` 前缀，再在 Windows 上用 `os.path.normcase()` 规范化。
- `_is_inside()` 与 `_relative_parts()`（`:89-113`）：先做普通 `relative_to()`；Windows 上失败后，再用规范化路径重试，保证包含关系判断和相对路径拆分采用一致口径。

`PathGuard._resolve_inside()` 仍先执行 `candidate.resolve(strict=False)`，再调用 `_is_inside()`。`resolve()` 会跟随符号链接和目录联接，因此“链接名字在 worktree 内”不等于“目标也在内”。

`test_windows_git_metadata_is_case_insensitive()` 明确要求 Windows 下 `.GIT/config` 和 `src/.Git/HEAD` 都抛 `GitWriteError`。这就是把安全边界写成回归测试，而不是只依赖人工说明。

### 3. 符号链接与目录联接测试怎么处理

`backend/tests/fsutil.py` 的 `symlink_or_skip()` 没有在整个 Windows 上一刀切跳过链接测试：

- 正常调用 `Path.symlink_to()`，能创建就真正执行测试。
- `FileExistsError` 和不在白名单内的 `OSError` 继续抛出，避免把真实缺陷伪装成 skip。
- 只有系统明确拒绝时才 skip：Windows `winerror == 1314`（`ERROR_PRIVILEGE_NOT_HELD`），或 `EPERM`、`EACCES`、`ENOTSUP`、`EOPNOTSUPP`。

这与 Windows 创建符号链接需要开发者模式或 `SeCreateSymbolicLinkPrivilege` 的环境限制相对应。`test_pathguard.py` 和 `test_runner.py` 都复用了这个辅助函数。

目录联接另有 `test_rejects_junction_that_points_outside_on_windows()`（`test_pathguard.py:117-137`）：非 Windows 跳过；Windows 上用参数列表执行 `cmd /c mklink /J`，随后验证通过联接读写 worktree 外路径都抛 `PathEscapeError`。如果系统拒绝创建联接或联接未出现，测试会记录原因并 skip。目录联接不需要与符号链接完全相同的创建权限，所以单独覆盖它有实际价值。

### 4. CI 加了什么

`.github/workflows/ci.yml:9-27` 的 `backend` job 使用矩阵：

```yaml
runs-on: ${{ matrix.os }}
strategy:
  matrix:
    os: [ubuntu-latest, windows-latest]
```

两种系统都执行 Python 3.11 环境准备、`python -m pip install -e ".[dev]"`、Ruff 和 Pytest。关键新增覆盖是 **Windows runner 真正跑同一套检查**，使 `taskkill` 路径、大小写规则以及能创建链接时的测试进入持续集成，而不是只在开发者的 Linux 环境验证。

`backend/tests/conftest.py` 还在测试仓库中设置 `core.autocrlf false`，避免 Windows 换行转换让新建 worktree 显得有未提交改动。`README.md` 新增“第一期在 Windows 上的限制”一段，说明进程树终止、大小写路径判断、符号链接权限与 skip 规则，以及 PR5 当时尚未实现网络隔离。

### 5. PR5 面试题

#### 题 1：Windows 上为什么不能用 `os.killpg`？这里怎样结束整棵进程树？

**参考答案：** `killpg` 是 POSIX 进程组接口，Windows 没有；`start_new_session` 在 Windows 也不能承担同样的分组作用。代码在 Windows 启动时设置 `CREATE_NEW_PROCESS_GROUP`，停止时由 `_stop_windows_tree()` 调用 `taskkill /F /T /PID <pid>`，其中 `/T` 遍历并结束进程树，最后再 `proc.kill()` 兜底。POSIX 则继续对独立会话使用 `os.killpg()`。

#### 题 2：为什么 `.GIT` 绕过 `.git` 检查是安全漏洞？修复涉及哪些函数？

**参考答案：** Windows 下不同大小写可指向同一路径，精确比较会让 `.GIT/config` 绕过 Git 元数据写保护，linked worktree 中还可能影响主仓库。`_touches_git()` 同时检查原始与解析后路径在 PR5 前就有；PR5 改用 `_relative_parts()` 拆分路径，并由 `_is_git_component()` 在 Windows 用 `casefold()` 比较。`_touches_secret()` 也改用 `_relative_parts()`；`_for_compare()`、`_is_inside()` 还统一处理大小写规范化和扩展路径前缀，避免包含关系与 Git 检查口径不一致。

#### 题 3：为什么不直接写 `skipif(Windows)` 跳过所有符号链接测试？CI 在这里起什么作用？

**参考答案：** 并非所有 Windows 环境都不能创建符号链接，一刀切会把本可运行的安全测试隐藏起来。`symlink_or_skip()` 只在权限不足或系统明确不支持时 skip，其他异常仍让测试失败；目录联接则由独立测试覆盖。CI 矩阵加入 `windows-latest` 后，这些 Windows 分支和通用回归测试会持续执行，减少“Linux 绿色但 Windows 已坏”的盲区。

## 三、实测结果

- Windows 本地：**138 passed / 8 skipped / 24 xfailed / 11 xpassed / 0 failed**。
- CI：Ubuntu 和 Windows 均通过。

---

复习时抓住两条主线：**PR4 用固定公式和边界测试保证排序正确、确定；PR5 按操作系统真实语义补齐进程树终止和路径安全边界。**
