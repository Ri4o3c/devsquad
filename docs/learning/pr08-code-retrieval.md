# PR #8：代码检索——ripgrep 召回 + BM25 排序

> 老板，本笔记只依据提交 `958e2b4` 的实际代码与测试；未改动仓库代码。

## 1. 两段式检索与 Planner 数据流

核心入口是 `search_code(root, query, *, keywords=None, top_k=8, backend="auto", max_file_bytes=1_000_000)`，返回 `list[SearchHit]`（`backend/src/devsquad/retrieval/code_search.py:195-203`）。

```text
query / keywords
  -> 生成并去重 words
  -> 第一阶段：ripgrep 文件级召回（或 Python 后备）
  -> 共用路径、大小、二进制过滤
  -> 切分代码块
  -> 第二阶段：BM25Index 对代码块打分、取 top_k
  -> SearchHit(path, start_line, end_line, score, snippet)
```

- **分工原因**：ripgrep 用固定字符串、忽略大小写快速缩小到候选文件；BM25 再按词频、文档频率和长度对代码块排序。前者解决“哪些文件可能相关”，后者解决“哪些片段最相关”（`code_search.py:218-281`）。多个 `-e` 关键词是 OR 召回，不要求同时出现（`code_search.py:237-247`）。
- **返回给上层的结构**：`SearchHit` 含相对路径、1 起始且两端包含的行号、分数和原文片段（`code_search.py:20-28`）。
- **与 Planner 的真实关系**：`backend/src/devsquad/retrieval/__init__.py:1-10` 导出 `SearchHit`、`search_code`，注释说明“为 Planner 提供代码片段”。但当前 `backend/src` 没有 Planner 对 `search_code` 的实际调用；因此本 PR 只提供检索接口，不能声称已接入 Planner 执行链。
- 中文需求应由调用方先改写成英文 `keywords`；本模块不调用模型（`code_search.py:1-3`、`retrieval/__init__.py:1-4`）。

## 2. 过滤规则

两条召回路径最终都经过 `_PathFilter.allowed()` 和 `_PathFilter.read()`（`code_search.py:121-160,252-255`）。

- **`.gitignore`**：按目录读取并缓存；支持注释、`!` 反选、目录规则、锚定路径、`*`、`?`、`**`；父目录被忽略后不再遍历（`code_search.py:36-84,96-145,163-169`）。ripgrep 关闭了 global、exclude、parent、dot 等外部忽略来源，最终仍由共用过滤器执行仓库内规则（`code_search.py:218-247`）。
- **直接阻止**：任一路径组件是符号链接；Windows 下解析路径与绝对路径不一致；`.git`、`.npmrc`、`.pypirc`（`code_search.py:87-93`）。
- **密钥形状**：复用 `is_secret_filename()`，大小写不敏感地阻止 `.env*`、`*.pem`、`*.key`、`*.p12`、`*.pfx`、`id_rsa*`、`id_dsa*`、`id_ecdsa*`、`id_ed25519*`；允许 `.env.example`、`.env.sample`、`.env.template`（`backend/src/devsquad/sandbox/pathguard.py:31-49,122-131`）。
- **路径与类型**：路径必须位于 `root` 内且不含 `..`；只读普通文件（`code_search.py:121-128,147-153`）。
- **大小**：大于 `max_file_bytes` 就跳过，默认 1,000,000 字节；读取时再次检查（`code_search.py:147-160,202`）。
- **二进制**：前 8192 字节含 NUL（`b"\0"`）就跳过；其余内容按 UTF-8 解码，错误字节替换，并统一 CRLF/CR 为 LF（`code_search.py:158-160,256`）。
- **隐藏文件不是一律忽略**：rg 使用 `--hidden`，测试也确认 `.hidden.txt` 可返回（`code_search.py:226`；`backend/tests/test_code_search.py:67,99`）。

## 3. 没有 rg 时的降级路径

- 默认 `backend="auto"` 用 `shutil.which("rg")` 探测；`backend="python"` 强制走后备（`code_search.py:209-211`）。
- 找不到 rg 时，`_files()` 用 `os.walk(root, followlinks=False)` 排序遍历，并提前剪掉不合规目录；读取和解码后，用忽略大小写的 `any(word in text)` 做文件级召回，之后仍走相同切分与 BM25（`code_search.py:163-169,248-258`）。
- 显式 `backend="rg"` 但找不到 rg，会抛 `RuntimeError`，不会自动降级（`code_search.py:210-211`）。
- rg 返回码 `0`、`1` 分别表示有匹配、无匹配；其他返回码抛 `RuntimeError`，也不会自动改走 Python（`code_search.py:239-247`）。

## 4. 切分、拆词与 BM25 打分

### 切分粒度

`_chunks()` 在 `code_search.py:172-192`：

- 可解析的 `.py`：按**顶层** `FunctionDef`、`AsyncFunctionDef`、`ClassDef` 切块；装饰器并入定义，定义之间的导入、常量等作为独立区间。类是完整一块，类方法不单独切。
- 非 Python 文件、AST 抛 `SyntaxError`/`ValueError`：每 50 行启动一个窗口，窗口最多 60 行，因此相邻窗口重叠 10 行。
- 空白片段不产出；`doc_id` 为 `relative:start-end`（`code_search.py:189-192,260-262`）。

### 标识符拆词

- `_expanded()` 拆开驼峰、acronym 边界和下划线，例如 `UserService -> User Service`、`parse_HTTPResponse -> parse HTTP Response`（`code_search.py:31-33`）。
- 未传 `keywords` 时，召回词来自 `tokenize(query) + tokenize(_expanded(query))`；随后去空白、去掉长度小于 2 的词并保序去重（`code_search.py:212-215`）。
- `bm25.py` 的 `tokenize()` 先转小写，再抽取 `[a-z0-9]+`（`backend/src/devsquad/retrieval/bm25.py:54-60`）。

### 与 `bm25.py` 的关系

- 每个代码块包装成 `Chunk` 加入同一个内存 `BM25Index()`；索引文本是“原片段 + 展开片段 + 相对路径 + 展开路径”，所以路径词也参与打分（`code_search.py:250-277`）。
- 排序查询同样拼接原查询、展开查询、召回词及其展开形式（`code_search.py:278-281`）。BM25 内会对查询词去重，重复拼接不会重复累计同一个查询词的得分（`bm25.py:123-124`）。
- 使用默认 `k1=1.5`、`b=0.75`；公式为 `idf=ln(1+(N-df+0.5)/(df+0.5))`，词项得分为 `idf * tf*(k1+1) / (tf+k1*(1-b+b*dl/avgdl))`（`bm25.py:75-90,112-116,129-145`）。
- 只返回正分，按分数降序、`doc_id` 升序破平，截取 `top_k`；最后映射回 `SearchHit`（`bm25.py:147-150`；`code_search.py:282-285`）。

## 5. 已知限制

- **中文不能独立召回**：`tokenize()` 只识别 `[a-z0-9]+`；纯中文且未提供有效 `keywords` 时返回 `[]`（`code_search.py:212-215`；`bm25.py:54-60`）。
- **召回是字面匹配**：rg 使用 `--fixed-strings`，Python 后备也是子串判断；没有语义或向量召回，向量与 RRF 明确留到第二期（`code_search.py:1,224-225,257`）。
- **每次重建内存索引**：每次 `search_code()` 都新建 `BM25Index` 并逐块 `add`，没有跨查询持久化或增量索引（`code_search.py:250-277`）。
- **rg 故障不降级**：子进程超时为 30 秒；非 `0/1` 返回码直接报错，只有“auto 且找不到 rg”才走 Python（`code_search.py:209-211,239-247`）。
- **过滤与切分是近似实现**：`GitIgnore` 是自定义规则子集；二进制只看前 8192 字节的 NUL；Python AST 只按顶层定义切块（`code_search.py:55-84,158-159,175-188`）。

## 6. 测试如何证明行为

测试文件：`backend/tests/test_code_search.py`。

- `test_gitignore`：14 组参数化用例覆盖注释、反选、目录规则、锚定、`*`、`?`、`**`（`:12-32`）。
- `test_gitignore_base`：子目录规则只作用于自己的 `base`，并验证反选（`:35-39`）。
- `test_search[python]`、`test_search[rg]`：验证首选结果、正分、两种后端的过滤结果、隐藏文件保留、CRLF 归一化、行号及结果确定性；夹具覆盖 `.git`、密钥、超大文件、NUL 二进制和 gitignore（`:42-109`）。
- `test_backend_parity`：rg 与 Python 返回完全相同的 `SearchHit` 列表（`:112-117`）。
- `test_auto_without_rg`：模拟找不到 rg，证明 auto 等价于 Python，显式 rg 则报错（`:120-124`）。
- `test_validation`：验证非法 `top_k`/`backend`、空词、纯中文、显式 `keywords`（`:127-135`）。
- `test_identifiers_and_chunks`：证明驼峰/下划线拆词、装饰器归入类块、顶层 async 函数行号（`:138-150`）。
- `test_windows_and_syntax_fallback`：证明语法错误时窗口降级、CR 归一化，以及 50/60 行窗口和 10 行重叠（`:153-164`）。
- `test_rg_errors`：模拟 rg 返回码 2，证明 stderr 进入 `RuntimeError`（`:167-175`）。
- `test_rg_no_matches`：证明无匹配返回 `[]`（`:178-180`）。
- `test_symlinks`：证明 Python 后端排除符号链接（`:183-193`）。
- `test_rg_single_root_call`：证明一次检索只调用一次 rg，并检查 `--no-require-git`、`--hidden` 和单一根路径参数（`:196-212`）。

## 7. 面试题与参考答案

### Q1：为什么不直接用 rg 输出结果，也不直接对全仓库做 BM25？

**答**：rg 擅长快速字面匹配，只负责文件级召回；BM25 擅长利用词频、逆文档频率和长度归一化做相关性排序。先缩小候选集，再对代码块排序，把“找得到”和“排得准”分开。代价是当前每次仍重建内存索引，且字面召回无法发现不含关键词的语义相关文件。

### Q2：`auto`、`rg`、`python` 三种后端在缺少 rg 时有何区别？

**答**：`auto` 通过 `shutil.which("rg")` 找不到时走 `_files()` 的 Python 遍历；`python` 始终走该路径；显式 `rg` 找不到可执行文件会抛 `RuntimeError`。rg 已存在但执行失败也不会自动降级。

### Q3：为什么查询 `user service` 能命中 `UserService`？

**答**：`_expanded()` 把驼峰、acronym 边界和下划线拆开，文档侧同时索引原文与展开文本，查询侧也加入原查询和展开查询；最终 `tokenize()` 转小写并抽取 `[a-z0-9]+`，所以双方都有 `user`、`service` 词项。

### Q4：Python 文件和普通文本的切分策略分别是什么？

**答**：可解析 Python 按顶层函数、async 函数和类切块，装饰器并入定义，间隙单独成块；普通文本或 AST 解析失败时，每 50 行开一个最多 60 行的窗口，形成 10 行重叠，降低边界切断上下文的影响。

### Q5：rg 已经遍历文件，为什么还需要 `_PathFilter`？

**答**：rg 只负责召回，不能替代项目的统一安全边界。候选路径仍要经过 `.gitignore`、符号链接、`.git`、密钥形状、大小和 NUL 二进制检查；Python 后备也复用同一过滤器，避免两条路径安全语义不一致。

## 实际执行的命令与结果

```bash
# 查看指定提交；退出码 0
git show 958e2b4

# 确认笔记目录和 rg；目录存在，rg 为 /usr/bin/rg
ls -ld /workspace/devsquad-docs/learning && command -v rg || true

# backend 无可执行 .venv/bin/python，按约定尝试系统 Python；失败
cd /workspace/devsquad-pr1/backend && if [ -x .venv/bin/python ]; then .venv/bin/python -m pytest; else python3 -m pytest; fi
# 结果：/usr/bin/python3: No module named pytest；退出码 1

# 在 /tmp 隔离环境安装项目及 dev 依赖，再运行完整测试；退出码 0
python3 -m venv /tmp/devsquad-pr8-pytest && /tmp/devsquad-pr8-pytest/bin/python -m pip install '/workspace/devsquad-pr1/backend[dev]' && cd /workspace/devsquad-pr1/backend && /tmp/devsquad-pr8-pytest/bin/python -m pytest
# collected 145 items
# 108 passed, 2 skipped, 24 xfailed, 11 xpassed, 1 warning in 1.72s

# 单独复跑本 PR 测试；退出码 0
cd /workspace/devsquad-pr1/backend && /tmp/devsquad-pr8-pytest/bin/python -m pytest tests/test_code_search.py -q
# 26 passed in 0.13s

# 确认仓库无改动；输出为空
git status --short
```
