"""Evaluate one-expression edits in two synthetic repositories with DeepSeek Flash."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from devsquad.agent.loop import ModelTurn, run_loop
from devsquad.tasks import create_task
from devsquad.tools.permissions import Decision
from devsquad.traces import TraceRecorder, trace_summary

MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
REPEATS = 3
MAX_OUTPUT = 192
RESERVED_PROMPT_TOKENS = 32768
MAX_BUDGET_CNY = 3.0
CNY_RESERVATION = (2 * RESERVED_PROMPT_TOKENS + 8 * MAX_OUTPUT) / 1_000_000
PROMPT = (
    "Repair exactly one Python function by changing only its return expression. "
    "Reply with a JSON object whose only key is expression and whose value is Python "
    "expression text. Use the named parameters and integer/boolean literals, arithmetic, "
    "comparisons, booleans or conditional expressions. No calls or attribute access."
)
ALLOWED_SYNTAX = (
    ast.Expression, ast.Name, ast.Load, ast.Constant, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv,
    ast.Mod, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
    ast.Gt, ast.GtE, ast.Lt, ast.LtE,
)


@dataclass(frozen=True)
class Case:
    repo: str
    name: str
    params: tuple[str, ...]
    original: str
    task: str
    checks: tuple[tuple[tuple, int | bool], ...]


CASES = (
    Case("arithmetic", "ceil_batches", ("items", "size"), "(items + size) // size",
         "Return the fewest batches of positive size needed for a non-negative item count.",
         (((0, 4), 0), ((4, 4), 1), ((8, 4), 2), ((9, 4), 3), ((3, 1), 3))),
    Case("arithmetic", "cap_quantity", ("quantity", "cap"), "cap if quantity < cap else quantity",
         "Clamp quantity to an upper bound of cap.",
         (((0, 4), 0), ((2, 4), 2), ((4, 4), 4), ((9, 4), 4))),
    Case("arithmetic", "remaining", ("total", "used"), "used - total",
         "Return total minus used. Keep negative amounts when used exceeds total.",
         (((10, 3), 7), ((3, 10), -7), ((0, 0), 0), ((1, 0), 1))),
    Case("arithmetic", "is_even", ("value",), "value % 2 == 1",
         "Return True for every even integer and False for every odd integer.",
         (((0,), True), ((8,), True), ((-5,), False), ((-8,), True), ((3,), False))),
    Case("store", "free_shipping", ("subtotal", "threshold"), "subtotal > threshold",
         "Return whether the subtotal reaches or exceeds the free-shipping threshold.",
         (((8, 9), False), ((9, 9), True), ((10, 9), True), ((0, 0), True))),
    Case("store", "can_discount", ("member", "subtotal"), "member and subtotal > 100",
         "Return True only when a member has a subtotal of at least 100.",
         (((True, 99), False), ((True, 100), True), ((True, 101), True), ((False, 100), False))),
    Case("store", "in_stock", ("available",), "available >= 0",
         "Return whether the available inventory is strictly positive.",
         (((-1,), False), ((0,), False), ((1,), True), ((100,), True))),
    Case("store", "can_return", ("age", "max_days"), "age < max_days",
         "Return whether the return age in days is no greater than max_days.",
         (((29, 30), True), ((30, 30), True), ((31, 30), False), ((0, 0), True))),
)


def approved_expression(text: str, case: Case) -> ast.Expression:
    if not isinstance(text, str) or len(text) > 180 or "\n" in text or "\r" in text:
        raise ValueError("Unsupported return expression")
    tree = ast.parse(text.strip(), mode="eval")
    nodes = list(ast.walk(tree))
    if len(nodes) > 48:
        raise ValueError("Expression exceeds complexity limit")
    for node in nodes:
        if not isinstance(node, ALLOWED_SYNTAX):
            raise ValueError("Unsupported expression syntax")
        if isinstance(node, ast.Name) and node.id not in case.params:
            raise ValueError("Unsupported variable")
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, bool) or abs(node.value) > 1000:
                raise ValueError("Unsupported constant")
    return tree


def check_expression(expression: str, case: Case) -> bool:
    tree = approved_expression(expression, case)
    code = compile(tree, "<validated expression>", "eval")
    try:
        for args, expected in case.checks:
            actual = eval(code, {"__builtins__": {}}, dict(zip(case.params, args, strict=True)))
            if type(actual) is not type(expected) or actual != expected:
                return False
    except ArithmeticError:
        return False
    return True


class RestrictedEdits:
    def __init__(self, case: Case) -> None:
        self.case = case

    def check(self, name: str, arguments: dict) -> Decision:
        if name == "edit_file" and arguments.get("path") == f"{self.case.name}.py":
            return Decision.ALLOW
        return Decision.DENY

    def edit(self, path: str, expression: str) -> str:
        target = Path(path)
        normalized = ast.unparse(approved_expression(expression, self.case))
        source = f"def {self.case.name}({', '.join(self.case.params)}):\n    return {normalized}\n"
        target.write_text(source, encoding="utf-8")
        return "expression replaced"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class DeepSeek:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls = 0
        self.metrics = {}
        self.opener = urllib.request.build_opener(NoRedirect())

    def peek_cost(self, messages: list[dict]) -> float:
        return (0.3 * RESERVED_PROMPT_TOKENS + 1.2 * MAX_OUTPUT) / 1_000_000

    def complete(self, messages: list[dict]) -> ModelTurn:
        request_body = json.dumps({
            "model": MODEL,
            "thinking": {"type": "disabled"},
            "max_tokens": MAX_OUTPUT,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": PROMPT}, *messages],
        }).encode()
        if len(request_body) > 4096 or (self.calls + 1) * CNY_RESERVATION > MAX_BUDGET_CNY:
            raise RuntimeError("Request or total budget limit reached")
        request = urllib.request.Request(
            ENDPOINT, data=request_body,
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        self.calls += 1
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=35) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DeepSeek HTTP {exc.code}; stopping without retry") from None
        except (OSError, ValueError):
            raise RuntimeError("DeepSeek request failed; stopping without retry") from None
        usage = result["usage"]
        input_count = usage["prompt_tokens"]
        output_count = usage["completion_tokens"]
        hits = usage.get("prompt_cache_hit_tokens", 0)
        if not (
            0 <= hits <= input_count <= RESERVED_PROMPT_TOKENS and 0 <= output_count <= MAX_OUTPUT
        ):
            raise RuntimeError("Measured token usage exceeded the assumed request bound")
        cny = (0.04 * hits + 2 * (input_count - hits) + 8 * output_count) / 1_000_000
        usd = (0.006 * hits + 0.3 * (input_count - hits) + 1.2 * output_count) / 1_000_000
        self.metrics = {
            "input_tokens": input_count,
            "cached_tokens": hits,
            "output_tokens": output_count,
            "peak_cost_cny": f"{cny:.8f}",
            "model_ms": round((time.monotonic() - started) * 1000),
            "returned_model": result["model"],
        }
        choice, = result["choices"]
        try:
            answer = json.loads(choice["message"]["content"] or "")
            if choice["finish_reason"] != "stop" or set(answer) != {"expression"}:
                raise ValueError("Nonconforming output")
            expression = answer["expression"]
            path = self.current_case.name + ".py"
        except (KeyError, TypeError, ValueError):
            return ModelTurn(usd, input_tokens=input_count, output_tokens=output_count)
        return ModelTurn(
            usd, tool_name="edit_file", tool_args={"path": path, "expression": expression},
            input_tokens=input_count, output_tokens=output_count,
        )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout


def fixture_repos(root: Path) -> dict[str, Path]:
    repos = {}
    for case in CASES:
        path = root / case.repo
        if case.repo not in repos:
            path.mkdir()
            git(path, "init", "-q")
            repos[case.repo] = path
        original = f"def {case.name}({', '.join(case.params)}):\n    return {case.original}\n"
        (path / f"{case.name}.py").write_text(original, encoding="utf-8")
        if check_expression(case.original, case):
            raise RuntimeError(f"{case.name} fixture already passes")
    for path in repos.values():
        git(path, "add", "--", "*.py")
        git(path, "-c", "user.name=DevSquad Eval", "-c", "user.email=eval@example.invalid",
            "commit", "-qm", "Synthetic evaluation fixture")
    return repos


def run_case(model: DeepSeek, case: Case, repeat: int, repo: Path, root: Path) -> tuple[dict, dict]:
    database = root / "runs.sqlite3"
    task = create_task(
        repo=repo, title=case.name, workspaces_dir=root / "worktrees", db_path=database,
    )
    worktree = Path(task.worktree_path)
    target = worktree / f"{case.name}.py"
    edits = RestrictedEdits(case)
    model.current_case = case
    recorder = TraceRecorder(database)
    result = run_loop(
        task=case.task + "\n\nCurrent code:\n" + target.read_text(encoding="utf-8"),
        model=model, tools={"edit_file": edits.edit}, permissions=edits, max_steps=1,
        max_cost_usd=0.05, repo_root=worktree,
        trace_recorder=recorder, task_id=task.id,
    )
    modified = git(worktree, "diff", "--name-only").splitlines()
    passed = False
    if modified == [target.name] and result.status == "step_limit":
        module = ast.parse(target.read_text(encoding="utf-8"))
        expression = ast.unparse(module.body[0].body[0].value)
        passed = check_expression(expression, case)
    assert not git(repo, "status", "--porcelain")
    row = {"repo": case.repo, "case": case.name, "repeat": repeat,
           "passed": int(passed), "loop_status": result.status, "steps": result.steps,
           **model.metrics}
    replay = {
        **row, "requirement": case.task,
        "events": [trace_summary(event) for event in recorder.list_events(task.id)],
        "diff": git(worktree, "--no-pager", "diff", "--no-ext-diff", "--", target.name),
    }
    return row, replay


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    settings = parser.parse_args()
    if len(CASES) * REPEATS * CNY_RESERVATION > MAX_BUDGET_CNY:
        raise SystemExit("Configured evaluation exceeds the maximum reservation")
    key = os.environ.pop("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("DEEPSEEK_API_KEY is missing")
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    model = DeepSeek(key)
    fields = (
        "repo", "case", "repeat", "passed", "loop_status", "steps", "input_tokens",
        "cached_tokens", "output_tokens", "peak_cost_cny", "model_ms", "returned_model",
    )
    replays = []
    with TemporaryDirectory(dir=settings.work_dir, prefix="deepseek-eval-") as scratch:
        root = Path(scratch)
        repos = fixture_repos(root)
        with settings.csv.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            for case in CASES:
                for repeat in range(1, REPEATS + 1):
                    row, replay = run_case(model, case, repeat, repos[case.repo], root)
                    writer.writerow(row)
                    handle.flush()
                    replays.append(replay)
                    print(f"{case.name} #{repeat}: passed={row['passed']}", flush=True)
    with settings.replay.open("x", encoding="utf-8") as handle:
        json.dump({"model": MODEL, "runs": replays}, handle, indent=2)


if __name__ == "__main__":
    main()
