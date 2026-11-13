"""子进程网络策略。

argv 拦截只防误操作，不是安全边界，拦不住 python -c 里的 socket。
真正的网络边界靠 Linux 的 user + net namespace；不可用时明确标记降级。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from enum import StrEnum
from functools import lru_cache
from pathlib import Path


class NetworkIsolation(StrEnum):
    """本次执行实际采用的网络隔离级别。"""

    NAMESPACE = "namespace"
    POLICY_ONLY = "policy_only"
    ALLOWED = "allowed"


def _subcommand(args: Sequence[str], value_options: set[str]) -> Sequence[str]:
    """跳过全局选项及其值，保留子命令和后续参数。"""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return args[i + 1 :]
        if not arg.startswith("-"):
            return args[i:]
        i += 2 if arg in value_options else 1
    return ()


def network_command_reason(argv: Sequence[str]) -> str | None:
    """识别明显联网的 argv；只防误操作，不能代替 OS 隔离。"""
    if not argv:
        return None
    name = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    args = argv[1:]
    blocked = name in {
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nc",
        "ncat",
        "netcat",
        "telnet",
        "ftp",
    }
    if name == "git":
        args = _subcommand(
            args,
            {
                "-C",
                "-c",
                "--git-dir",
                "--work-tree",
                "--namespace",
                "--exec-path",
                "--config-env",
                "--super-prefix",
            },
        )
        blocked = bool(
            args
            and args[0]
            in {
                "push",
                "fetch",
                "pull",
                "clone",
                "remote",
                "ls-remote",
                "submodule",
            }
        )
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|py", name):
        # -c、脚本等不在此解析；只识别启动参数后的 -m pip。
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-m":
                if len(args) > i + 1 and args[i + 1] == "pip":
                    name, args = "pip", args[i + 2 :]
                break
            if args[i] in {"-c", "--"}:
                break
            i += 2 if args[i] in {"-W", "-X"} else 1
    if name in {"pip", "pip3"}:
        args = _subcommand(
            args,
            {
                "--python",
                "--proxy",
                "--timeout",
                "--retries",
                "--cache-dir",
                "--log",
                "--cert",
                "--client-cert",
            },
        )
        blocked = bool(args and args[0] in {"install", "download"})
    if name in {"npm", "pnpm", "yarn"}:
        args = _subcommand(args, {"--cwd", "--prefix", "--dir", "-C", "--registry"})
        blocked = bool(args and args[0] in {"install", "i", "ci", "add"})
        # yarn 不带子命令也会安装依赖。
        blocked |= name == "yarn" and not args
    if name == "uv":
        args = _subcommand(args, {"--directory", "--project", "--config-file"})
        blocked = bool(
            args
            and (
                args[0] in {"add", "sync"}
                or (args[0] == "pip" and len(args) > 1 and args[1] == "install")
            )
        )
    if name in {"cargo", "go"}:
        args = _subcommand(args, {"-C", "--manifest-path"})
        blocked = bool(args and args[0] in {"install", "get", "fetch"})
    if blocked:
        return "默认禁止联网：该命令可能访问网络；依赖安装需单独确认并显式允许网络"
    return None


@lru_cache(maxsize=1)
def network_namespace_command() -> tuple[str, ...] | None:
    """真实探测非特权 user/net namespace，结果按进程缓存；失败则降级。"""
    if sys.platform != "linux":
        return None
    unshare = shutil.which("unshare")
    if unshare is None:
        return None
    prefix = (str(Path(unshare).resolve()), "--user", "--map-root-user", "--net", "--")
    try:
        result = subprocess.run(
            [*prefix, sys.executable, "-c", "pass"],
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return prefix if result.returncode == 0 else None


def clear_network_probe_cache() -> None:
    """清除探测缓存，供测试或运行环境变更后使用。"""
    network_namespace_command.cache_clear()
