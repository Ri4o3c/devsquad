"""Run a child process inside a task worktree.

The runner is a baseline, not a container:

* The command is an argument list. ``shell`` is hard-coded off and there is
  no parameter that turns it on. A string command is rejected, so ``&&``
  cannot be composed by accident.
* ``cwd`` has to pass :class:`~devsquad.sandbox.pathguard.PathGuard` and be
  an existing directory inside the worktree.
* The child gets an allowlisted environment. Parent API keys and tokens are
  not copied.
* A timeout and a per-stream output cap are enforced. On timeout the
  child's process group is killed, not only the direct child. POSIX uses
  ``killpg`` after ``start_new_session``. Windows uses
  ``CREATE_NEW_PROCESS_GROUP`` and ``taskkill /F /T``.

TODO(network): deny outbound network for child processes. Do not implement
this by setting ``HTTP_PROXY`` or ``NO_PROXY``. A program can ignore those
variables and open a socket. A later PR should use an OS facility (Linux
network namespaces, or a local firewall) and prove that a real ``connect()``
fails. This module does not pretend the network is closed.

Filesystem isolation has the same shape of gap. The path guard applies to
``cwd`` and to paths the application checks itself. The child can still
open an absolute path. Treat that as out of scope for this PR.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from devsquad.sandbox.pathguard import PathGuard, SandboxError

# Copied into the child when present. Anything else is dropped, including
# OPENAI_API_KEY, GITHUB_TOKEN, AWS credentials, and DEVSQUAD_TOKEN.
_ENV_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TEMP",
    "TMP",
    "USER",
    "LOGNAME",
    # Windows cannot start most programs without these. They are not secrets.
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
    "USERPROFILE",
)

_SECRET_KEY_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


class CommandRejected(SandboxError):
    """The command was not started."""


@dataclass(frozen=True)
class RunResult:
    """Captured output of one child process.

    ``stdout`` and ``stderr`` are each truncated to the configured cap.
    ``timed_out`` means the process group was killed because the clock ran
    out. ``truncated`` means at least one stream hit the byte cap.
    """

    args: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    duration_s: float


def looks_like_secret_env(key: str) -> bool:
    """True when an environment variable name looks like a credential."""
    upper = key.upper()
    return any(marker in upper for marker in _SECRET_KEY_MARKERS)


def minimal_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the environment a child process is allowed to see.

    Only :data:`_ENV_ALLOW` is copied, and a key is still dropped when its
    name looks like a secret. That second check matters if someone later
    adds ``AWS_SECRET_ACCESS_KEY`` to the allowlist. Values are not scanned;
    a secret embedded inside ``PATH`` would still leak, which is a known
    limit of this baseline.
    """
    src = os.environ if source is None else source
    env: dict[str, str] = {}
    for key in _ENV_ALLOW:
        if key in src and not looks_like_secret_env(key):
            env[key] = src[key]
    env.setdefault("LANG", "C.UTF-8")
    return env


class CommandRunner:
    """Execute argument lists with cwd locked to one worktree."""

    def __init__(
        self,
        worktree: Path,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.guard = PathGuard(worktree)
        self.timeout_s = float(timeout_s)
        self.max_output_bytes = int(max_output_bytes)

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str | Path | None = None,
        timeout_s: float | None = None,
    ) -> RunResult:
        """Run ``args`` and return the captured result.

        ``args`` must be a sequence of strings. A ``str`` is rejected even
        though ``str`` is a sequence, because passing one string is how
        shell commands usually sneak in.
        """
        argv = _validate_args(args)
        timeout = self.timeout_s if timeout_s is None else timeout_s
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")
        cwd_path = self._cwd(cwd)
        env = minimal_env()

        started = time.monotonic()
        # shell is a literal False. It is not a parameter.
        # On Windows start_new_session is ignored, so the process group has
        # to be requested with CREATE_NEW_PROCESS_GROUP instead.
        popen_kwargs: dict = {"shell": False}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            cwd=cwd_path,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        truncated = threading.Event()
        assert proc.stdout is not None and proc.stderr is not None
        out_thread = threading.Thread(
            target=_pump,
            args=(proc.stdout, stdout_buf, self.max_output_bytes, truncated),
            daemon=True,
        )
        err_thread = threading.Thread(
            target=_pump,
            args=(proc.stderr, stderr_buf, self.max_output_bytes, truncated),
            daemon=True,
        )
        out_thread.start()
        err_thread.start()

        timed_out = False
        deadline = started + timeout
        try:
            while proc.poll() is None and not truncated.is_set():
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.01)
            if proc.poll() is None:
                _stop_process_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process_group(proc)
                proc.wait(timeout=5)
        finally:
            # Join before closing so a normal exit can finish draining the
            # pipes. Closing first drops bytes the threads have not read yet.
            out_thread.join(timeout=2)
            err_thread.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

        return RunResult(
            args=tuple(argv),
            returncode=proc.returncode,
            stdout=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
            timed_out=timed_out,
            truncated=truncated.is_set(),
            duration_s=time.monotonic() - started,
        )

    def _cwd(self, cwd: str | Path | None) -> Path:
        if cwd is None:
            path = self.guard.root
        else:
            path = self.guard.check_read(cwd)
        if not path.is_dir():
            raise CommandRejected(f"cwd is not a directory: {path}")
        try:
            path.relative_to(self.guard.root)
        except ValueError as exc:
            raise SandboxError(f"cwd escapes the worktree: {path}") from exc
        return path


def _validate_args(args: Sequence[str]) -> list[str]:
    if isinstance(args, (str, bytes)):
        raise CommandRejected(
            "command must be an argument list, not a string; shell execution is not supported"
        )
    if not isinstance(args, Sequence):
        raise CommandRejected("command must be a sequence of strings")
    argv = list(args)
    if not argv:
        raise CommandRejected("command is empty")
    if not all(isinstance(part, str) for part in argv):
        raise CommandRejected("every command argument must be a string")
    if any("\0" in part for part in argv):
        raise CommandRejected("command arguments must not contain NUL")
    return argv


def _pump(stream, buf: bytearray, limit: int, truncated: threading.Event) -> None:
    """Read ``stream`` into ``buf`` and stop once ``limit`` bytes are kept."""
    try:
        while not truncated.is_set():
            chunk = stream.read(65536)
            if not chunk:
                return
            room = limit - len(buf)
            if room <= 0:
                truncated.set()
                return
            if len(chunk) > room:
                buf.extend(chunk[:room])
                truncated.set()
                return
            buf.extend(chunk)
    except (OSError, ValueError):
        # The parent closed the pipe or killed the child. Captured bytes stay.
        return


def _stop_process_group(proc: subprocess.Popen) -> None:
    """Kill the child and anything it spawned in its session.

    ``Popen.kill`` signals only the direct child. A timeout that left
    grandchildren running would not be a timeout. POSIX puts the child in
    its own session and signals that group. Windows has no ``killpg``;
    ``taskkill /T`` walks the process tree instead.
    """
    if os.name == "nt":
        _stop_windows_tree(proc)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.kill()
        except ProcessLookupError:
            return


def _stop_windows_tree(proc: subprocess.Popen) -> None:
    """End ``proc`` and its children. ``taskkill`` is an argument list, not a shell."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
        capture_output=True,
        check=False,
        creationflags=flags,
    )
    try:
        proc.kill()
    except OSError:
        return
