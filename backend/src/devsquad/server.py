"""Loopback FastAPI app with a token-protected local run API."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from devsquad.attachments import (
    MAX_ATTACHMENTS,
    MAX_ENCODED_FILE,
    MAX_UPLOAD_BODY,
    AttachmentError,
    prepare_attachment,
)
from devsquad.config import db_path, server_token, workspaces_dir
from devsquad.tasks import TaskError
from devsquad.web_runs import RunConflict, RunManager, RunUnavailable

BIND_HOST = "127.0.0.1"
TOKEN_HEADER = "x-devsquad-token"
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:[0-9]{1,5})?$")
_ACTIVE = {"starting", "running", "waiting_approval"}


@asynccontextmanager
async def lifespan(application: FastAPI):
    try:
        yield
    finally:
        manager = getattr(application.state, "run_manager", None)
        if manager is not None:
            manager.close()


app = FastAPI(
    title="DevSquad", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
)


def _host_allowed(value: str) -> bool:
    match = _HOST_RE.fullmatch(value.strip().lower())
    if match is None:
        return False
    port = match.group(2)
    return port is None or 1 <= int(port[1:]) <= 65535


def _origin_allowed(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.username is None and parsed.password is None
        and parsed.scheme in {"http", "https"} and parsed.hostname in _ALLOWED_HOSTS
        and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


def authorize(request: Request) -> JSONResponse | None:
    host = request.headers.get("host", "")
    if not _host_allowed(host):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None:
        if not _origin_allowed(origin) or (
            request.url.path.startswith("/api/") and origin.lower() != f"http://{host.lower()}"
        ):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
    token = request.headers.get(TOKEN_HEADER, "")
    expected = server_token()
    if not token or not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return None


@app.middleware("http")
async def enforce_local_client(request: Request, call_next):
    path = request.url.path
    if getattr(request.app.state, "web_root", None) is not None and (
        path in {"/", "/benchmark.json"} or path.startswith("/assets/")
    ):
        if not _host_allowed(request.headers.get("host", "")):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "style-src 'self' 'unsafe-inline'"
        )
        return response
    rejected = authorize(request)
    if rejected is not None:
        return rejected
    if path.startswith("/api/") and request.method == "POST":
        length = request.headers.get("content-length", "")
        if not re.fullmatch(r"[0-9]{1,7}", length):
            return JSONResponse({"detail": "需要有效的请求长度。"}, status_code=411)
        if int(length) > (MAX_UPLOAD_BODY if path == "/api/runs" else 40_960):
            return JSONResponse({"detail": "请求内容过大。"}, status_code=413)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"detail": "需要 JSON 请求。"}, status_code=415)
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


class AttachmentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    data: str = Field(min_length=4, max_length=MAX_ENCODED_FILE)


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    repo: str = Field(min_length=1, max_length=2048)
    task: str = Field(min_length=1, max_length=6000)
    max_steps: int = Field(8, strict=True, ge=1, le=24)
    max_cost_usd: float = Field(0.05, strict=True, gt=0, allow_inf_nan=False)
    attachments: list[AttachmentPayload] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: StrictBool


def manager_for(request: Request) -> RunManager:
    manager = getattr(request.app.state, "run_manager", None)
    if manager is None:
        raise HTTPException(503, "运行服务尚未启动。")
    return manager


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"detail": "请求参数不符合要求。"}, status_code=422)


@app.get("/api/runs/current")
def current_run(request: Request) -> dict:
    return {"id": manager_for(request).latest()}


@app.post("/api/runs", status_code=202)
def start_run(body: StartRun, request: Request) -> dict:
    repo = Path(body.repo)
    if not repo.is_absolute() or "\0" in body.repo:
        raise HTTPException(422, "请输入本机后端可访问的仓库绝对路径。")
    try:
        attachments = tuple(prepare_attachment(item.name, item.data) for item in body.attachments)
        run_id = manager_for(request).start(
            repo, body.task, body.max_steps, body.max_cost_usd, attachments,
        )
    except AttachmentError as exc:
        raise HTTPException(422, str(exc)) from None
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except RunUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    return {"id": run_id}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    manager = manager_for(request)
    try:
        first = await asyncio.to_thread(manager.snapshot, run_id)
    except KeyError:
        raise HTTPException(404, "找不到这次运行。") from None

    async def stream():
        snapshot = first
        previous = None
        while True:
            if snapshot != previous:
                yield "data: " + json.dumps(snapshot, ensure_ascii=False) + "\n\n"
                previous = snapshot
            if snapshot["status"] not in _ACTIVE:
                return
            await asyncio.sleep(0.4)
            try:
                snapshot = await asyncio.to_thread(manager.snapshot, run_id)
            except KeyError:
                return

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/approvals/{approval_id}")
def decide(run_id: str, approval_id: str, body: ApprovalDecision, request: Request) -> dict:
    try:
        manager_for(request).decide(run_id, approval_id, body.approve)
    except KeyError:
        raise HTTPException(404, "找不到这次运行。") from None
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {"ok": True}


@app.get("/api/runs/{run_id}/diff")
def run_diff(run_id: str, request: Request) -> dict:
    try:
        return manager_for(request).patch(run_id)
    except KeyError:
        raise HTTPException(404, "找不到这次运行。") from None
    except RunConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (OSError, ValueError, TaskError):
        raise HTTPException(409, "无法读取任务工作区的变更。") from None


def web_file(request: Request, relative: str) -> FileResponse:
    root = getattr(request.app.state, "web_root", None)
    if root is None:
        raise HTTPException(404)
    path = root / relative
    if path.resolve() != path or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/")
def index(request: Request):
    return web_file(request, "index.html")


@app.get("/benchmark.json")
def example(request: Request):
    return web_file(request, "benchmark.json")


@app.get("/assets/{filename}")
def asset(filename: str, request: Request):
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:js|css)", filename):
        raise HTTPException(404)
    return web_file(request, "assets/" + filename)


def serve(port: int = 8765, *, web_root: Path | None = None) -> None:
    import uvicorn

    if port <= 0 or port > 65535:
        raise ValueError("port out of range")
    root = web_root.resolve() if web_root is not None else None
    if root is not None:
        if not (root / "index.html").is_file():
            raise ValueError("先运行前端构建，然后传入生成的 dist 目录。")
        if not server_token():
            raise ValueError("启动网页工作台需要配置 DEVSQUAD_TOKEN。")
    app.state.web_root = root
    app.state.run_manager = RunManager(db_path(), workspaces_dir())
    uvicorn.run(app, host=BIND_HOST, port=port, log_level="info", timeout_graceful_shutdown=3)
