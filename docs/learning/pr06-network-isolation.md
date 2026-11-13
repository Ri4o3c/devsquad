# PR 6：沙箱命令默认禁止联网

> 对应提交：`ec63e86`。核心结论：**argv 拦截只防误操作，真正的网络边界是 Linux user + network namespace；无法强制隔离时必须明确降级，不能假装安全。**

## 1. 为什么要默认禁网

DevSquad 会在 worktree 中执行构建、测试和模型给出的命令，而这些操作本质上可能执行仓库中的任意代码。默认联网会带来两类风险：

- 恶意或误生成的命令访问外部服务，例如 `git push`、包管理器安装、下载脚本。
- 任意代码通过 socket 外发源码或它仍能读到的信息。只拦 `curl` 拦不住 `python -c` 里的 socket。

因此 PR 6 采用两层防线：

1. 启动前用 argv 规则拦截明显联网命令。
2. Linux 可用时，用 user + network namespace 限制整个子进程的网络。

依赖安装不混入普通命令执行：需要人工确认后，通过 `allow_network=True` 显式开放。设计依据见 `docs/design.md` 6.5 节，尤其是 `docs/design.md:715-723`。

注意：`POLICY_ONLY` 不是恶意代码安全边界；worktree 和路径守卫也不是网络或文件系统隔离边界。

## 2. 一次命令执行经过哪些检查

核心入口是 `CommandRunner.run`，见 `backend/src/devsquad/sandbox/runner.py:138-239`：

1. `_validate_args` 校验参数必须是字符串序列，拒绝单个字符串、空 argv、非字符串元素和 NUL；实际启动固定 `shell=False`。
2. `allow_network=True`：不执行联网命令拦截，也不探测 namespace，结果为 `ALLOWED`。
3. 默认禁网时，先调用 `network_command_reason(argv)`；命中规则就抛出 `CommandRejected`，目标进程不会启动。
4. 未命中规则时调用 `network_namespace_command()`：
   - 返回前缀：给原 argv 加上 `unshare` 前缀，结果为 `NAMESPACE`。
   - 返回 `None`：默认以 `POLICY_ONLY` 继续；若 `require_network_isolation=True`，则拒绝启动。
5. 返回 `RunResult` 时，`args` 仍保存原始 argv，`network` 记录本次实际隔离级别。

## 3. argv 拦截：`network_command_reason`

实现位于 `backend/src/devsquad/sandbox/network.py:27-135`。

### 3.1 可执行文件名规范化

- 将 `\` 统一为 `/`，取最后一段并转小写。
- 去掉末尾的 `.exe`、`.cmd` 或 `.bat`。
- 因此 `GIT.EXE`、`C:\Git\git.EXE`、`npm.cmd` 等也能进入同一套规则。
- 空 argv 返回 `None`；命中时返回拒绝原因，未命中也返回 `None`。

### 3.2 实际规则

| 命令 | 拦截范围 |
| --- | --- |
| `curl`、`wget`、`ssh`、`scp`、`sftp`、`rsync`、`nc`、`ncat`、`netcat`、`telnet`、`ftp` | 可执行文件名命中即拦截 |
| `git` | 跳过全局选项后，拦截 `push`、`fetch`、`pull`、`clone`、`remote`、`ls-remote`、`submodule` |
| `python` / `py` | 只识别启动参数中的 `-m pip`，再按 pip 规则处理；`-W`、`-X` 带值，遇到 `-c` 或 `--` 停止解析 |
| `pip`、`pip3` | 跳过全局选项后，拦截 `install`、`download` |
| `npm`、`pnpm`、`yarn` | 拦截 `install`、`i`、`ci`、`add`；`yarn` 不带子命令也视为安装 |
| `uv` | 拦截 `add`、`sync`、`pip install` |
| `cargo`、`go` | 拦截 `install`、`get`、`fetch` |

`_subcommand` 的作用是跳过全局选项及其值，避免把 `git -C x push` 中的 `x` 当成子命令，也避免把 `git -C push status` 中的 `push` 误判为联网操作。

### 3.3 局限：它不是安全边界

- 不解析 `python -c`、Python 脚本或其他任意代码；这些代码可以直接创建 socket。
- `npm run test`、`uv run pytest` 等本地命令会放行，但它们最终执行的脚本仍可能联网。
- 它只覆盖写明的命令和子命令，不能理解所有包装器、别名或脚本行为。
- 所以 `POLICY_ONLY` 只能减少误操作；要限制任意代码，必须依赖 `NAMESPACE` 或更强的外部沙箱。

这些局限直接写在 `backend/src/devsquad/sandbox/network.py:1-5` 和 `backend/src/devsquad/sandbox/runner.py:1-7`，不是额外假设。

## 4. Linux 真隔离：`unshare --user --map-root-user --net`

### 4.1 三个参数分别做什么

实际前缀构造见 `backend/src/devsquad/sandbox/network.py:138-159`：

```text
<unshare 的绝对路径> --user --map-root-user --net --
```

- `--user`：创建新的 user namespace。
- `--map-root-user`：把当前用户映射为新 user namespace 内的 root，使其在该 namespace 内拥有所需能力；**不等于获得宿主机 root 权限**。
- `--net`：创建新的 network namespace。子进程不再共享宿主机原来的网络接口、地址和路由，默认不能沿用宿主机网络访问外部。
- `--`：结束 unshare 选项，后面接原始 argv。

这里没有传 `--fork`：`unshare` 直接 `exec` 目标程序，目标进程 PID 与 `Popen` 返回的 PID 保持一致，已有的进程组超时清理逻辑仍然有效。对应说明在 `backend/src/devsquad/sandbox/runner.py:160-164`；`test_timeout_kills_grandchild` 进一步验证了默认隔离下的孙进程超时清理，见 7.1 节。

### 4.2 真实探测，而不是只看系统名字

`network_namespace_command()` 的探测过程：

1. 非 Linux 直接返回 `None`。
2. 用 `shutil.which("unshare")` 查找工具；找不到返回 `None`。
3. 将 unshare 路径解析为绝对路径，真实执行：

   ```text
   unshare --user --map-root-user --net -- <sys.executable> -c pass
   ```

4. 探测使用 `subprocess.run`，`env={}`，标准输入/输出/错误均接到 `DEVNULL`，`timeout=3`，`check=False`。
5. 返回码为 0 才返回前缀；非零、`OSError` 或 `TimeoutExpired` 都返回 `None`。

这能覆盖“有 unshare 但内核或 AppArmor 禁止非特权 userns”的情况，避免把“命令存在”误当成“隔离可用”。

### 4.3 缓存与降级

- `@lru_cache(maxsize=1)` 按进程缓存探测结果，成功和失败都会缓存，避免每次运行都启动探测进程。
- `clear_network_probe_cache()` 清除缓存，供测试或运行环境变化后使用，见 `backend/src/devsquad/sandbox/network.py:162-164`。
- 探测失败后的默认行为是 `POLICY_ONLY`，并通过 `RunResult.network` 暴露事实；需要强保证时用 `require_network_isolation=True` 改为 fail closed。

## 5. 三档状态与两个开关

`NetworkIsolation` 是 `StrEnum`，定义在 `backend/src/devsquad/sandbox/network.py:19-24`。

| 值 | 含义 | 是否能限制任意 socket |
| --- | --- | --- |
| `NAMESPACE` | argv 规则通过后，已在 user + network namespace 中执行 | 能在本机制覆盖范围内强制隔离 |
| `POLICY_ONLY` | 只有 argv 拦截，OS 隔离不可用 | 不能 |
| `ALLOWED` | 调用方显式允许联网 | 不限制 |

`CommandRunner.__init__` 的两个参数见 `backend/src/devsquad/sandbox/runner.py:119-136`：

| 参数组合 | 行为 |
| --- | --- |
| 默认：`allow_network=False`、`require_network_isolation=False` | 先拦截明显联网命令；有 namespace 就强隔离，没有则降级为 `POLICY_ONLY` |
| `require_network_isolation=True` | 无可用 namespace 时抛出 `CommandRejected`，不启动目标进程，即 fail closed |
| `allow_network=True` | 跳过 argv 拦截和 namespace 探测，结果为 `ALLOWED` |
| 两者都为 `True` | 显式允许网络优先，结果为 `ALLOWED` |

`RunResult.network` 字段定义在 `backend/src/devsquad/sandbox/runner.py:69-85`。它让调用方能区分“真的隔离了”和“只做了策略拦截”，而不是用一个布尔值掩盖降级。

相关公共 API 已从 `backend/src/devsquad/sandbox/__init__.py:6-37` 导出，包括 `NetworkIsolation`、`network_command_reason`、`network_namespace_command`、`clear_network_probe_cache`、`CommandRunner` 和 `RunResult`。

## 6. 各平台现状

| 平台 / 环境 | 默认结果 | 强制隔离能力 | `require_network_isolation=True` |
| --- | --- | --- | --- |
| Linux，unshare 存在且真实探测成功 | `NAMESPACE` | user + network namespace | 正常执行 |
| Linux，缺少 unshare，或内核 / AppArmor 禁止，或探测失败 / 超时 | `POLICY_ONLY` | 无，仅 argv 拦截 | 拒绝启动 |
| Windows | `POLICY_ONLY` | 第一期未实现非特权网络隔离；防火墙规则 / AppContainer 未落地 | 拒绝启动 |
| macOS | `POLICY_ONLY` | 当前同样降级，仅 argv 拦截 | 拒绝启动 |

任何平台上，`allow_network=True` 都显式走 `ALLOWED`。平台说明也见 `README.md:100-104`；README 的阶段表在 `README.md:24` 将默认禁网标为已实现，并强调会标记实际隔离级别。

## 7. 测试怎么证明不是“嘴上禁网”

测试集中在 `backend/tests/test_network.py`。

### 7.1 规则与执行顺序

- `test_network_commands`：覆盖联网工具、Git 子命令、Windows 后缀和路径、包管理器及全局选项。
- `test_local_commands`：确认 `git status`、`python -c`、`npm run test`、`pip list`、`uv run pytest` 等不会被 argv 规则误拦。
- `test_rejects_before_any_process_starts`：对 `git push`、`curl`、`npm i`，断言抛出 `CommandRejected` 且 `Popen` 未调用。
- `test_allow_network_bypasses_policy`：显式开放网络时不调用 namespace 探测；还运行一个名为 `curl` 的不存在路径并预期 `FileNotFoundError`，证明命令名拦截也被跳过。
- `test_policy_only_and_fail_closed`：模拟探测返回 `None`，默认执行并返回 `POLICY_ONLY`；开启 `require_network_isolation` 后拒绝执行。
- `test_probe_cached_and_degrades`：覆盖成功、非零返回、`OSError`、超时；连续调用两次只探测一次，并核对真实探测 argv 和 3 秒超时。
- `test_probe_without_support`：覆盖非 Linux及找不到 unshare。
- `test_namespace_exec_keeps_pid`：真实验证不加 `--fork` 时 PID 保持不变。
- `test_timeout_kills_grandchild`（`test_network.py:260-278`）：默认隔离下启动会延迟写文件的孙进程，超时后断言 `timed_out` 且标记文件不存在，支撑 4.1 节“不加 `--fork` 时进程组清理仍有效”。

### 7.2 为什么只连 `127.0.0.1` 反而能证明隔离

`local_server` 在父进程原来的 network namespace 中绑定 `127.0.0.1` 和随机端口，见 `backend/tests/test_network.py:118-150`。`_connect_command` 让子进程用 Python socket 连接这个地址，见 `backend/tests/test_network.py:153-166`。

`test_namespace_blocks_real_connect` 的断言是：

- 子进程确实以 `NAMESPACE` 运行；
- 连接抛出 `OSError`，测试程序按约定退出码为 `23`；
- 父进程服务器的 `accepted` 事件没有被设置。

关键点是：**子进程里的 `127.0.0.1` 指向它自己的新 network namespace，不是父进程监听 socket 所在的 network namespace。** 新 netns 里只有 `lo`，且默认是 DOWN，所以连子进程自己的 `127.0.0.1` 也不可达。因而无需访问公网，就能确定性地证明网络 namespace 生效。

作为正对照，`test_allowed_real_connect` 使用完全相同的本机连接方式，但设置 `allow_network=True`，断言退出码为 0、状态为 `ALLOWED`，且服务器确实接受连接。这个对照排除了“测试程序本身根本不会连接”的解释。

这组测试还故意使用 `python -c` 直接创建 socket，证明 OS 隔离能补足 argv 解析的盲区。不过 `require_namespace()` 会在环境不支持时跳过真实 namespace 测试；父进程无法绑定本机 socket 时也会跳过，所以跳过不能算作隔离验证通过。

## 8. 面试时的 30 秒总结

“我把默认禁网拆成策略层和 OS 层。策略层解析 argv，拦截 Git、包管理器和常见联网工具，但明确承认它拦不住任意 Python socket。Linux 上我真实探测非特权 user + network namespace，成功就用 unshare 强制隔离，失败则返回 `POLICY_ONLY`，并允许调用方用 `require_network_isolation=True` 选择 fail closed。测试不依赖公网，而是用父子 network namespace 中不同的 `127.0.0.1` 做真实 connect 验证，再用 `allow_network=True` 做成功对照。”

## 9. 面试题与参考答案

### 1. 为什么拦了 `curl`、`wget`，还不能说命令已经禁网？

因为联网不是某个可执行文件名的专属能力。`python -c`、项目脚本和测试代码都能直接创建 socket。`network_command_reason` 只识别明显联网的 argv，属于防误操作；PR 6 在 Linux 上用 network namespace 才形成任意代码也受约束的网络边界。代码：`backend/src/devsquad/sandbox/network.py:40-135`。

### 2. `--user --map-root-user --net` 为什么适合非特权禁网？它会不会让进程变成宿主机 root？

`--user` 创建 user namespace，`--map-root-user` 只把当前用户映射为该 namespace 内的 root，不会获得宿主机 root 权限；`--net` 再创建独立 network namespace，使子进程不能沿用宿主机原来的网络接口和路由。实现：`backend/src/devsquad/sandbox/network.py:138-159`。

### 3. 为什么必须真实执行一次探测？为什么还要缓存？

仅检查 Linux 或 unshare 是否存在，无法发现内核 / AppArmor 禁止 userns、执行报错或超时。因此代码用当前 Python 在目标 namespace 参数下执行 `-c pass`，返回码为 0 才认为可用。探测要启动进程，所以用 `@lru_cache(maxsize=1)` 缓存成功和失败；环境变化或测试时调用 `clear_network_probe_cache()`。代码：`backend/src/devsquad/sandbox/network.py:138-164`。

### 4. 默认降级和 fail closed 矛盾吗？怎么选择？

不矛盾。默认模式偏可用性：隔离不可用时以 `POLICY_ONLY` 执行，并通过 `RunResult.network` 明确暴露降级。对“没有 OS 隔离就绝不能跑”的场景，设置 `require_network_isolation=True`，无 namespace 就抛出 `CommandRejected`。若 `allow_network=True`，表示调用方明确接受联网，优先于强隔离要求。代码：`backend/src/devsquad/sandbox/runner.py:151-164`。

### 5. 测试为什么连接 `127.0.0.1`，而不是访问一个公网 URL？

本机测试确定、无外部依赖，也能直接验证 namespace 边界。父进程在原 namespace 监听 `127.0.0.1:<随机端口>`；隔离子进程的 loopback 属于新 namespace，连接不到父进程监听地址，所以退出 `23` 且服务器未 accept。相同命令在 `allow_network=True` 时成功并被 accept，形成正对照。代码：`backend/tests/test_network.py:118-193`。

## 10. 实测结果

- Windows 本地：**138 passed / 8 skipped / 24 xfailed / 11 xpassed / 0 failed**。
- CI：Ubuntu 和 Windows 均通过。
