"""FastAPI server: API endpoints, auth, static dashboard, worker lifecycle."""

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import Config, load_config
from .db import Database
from .dispatcher import validate_github_url, repo_name_from_url
from .worker import WorkerPool

log = logging.getLogger("compound")

# Module-level state
_config: Config | None = None
_db: Database | None = None
_pool: WorkerPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config, _db, _pool

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    _config = load_config()
    _db = Database(_config.db_path)
    await _db.connect()
    log.info("Database connected: %s", _config.db_path)

    _config.repos_dir.mkdir(parents=True, exist_ok=True)
    _config.logs_dir.mkdir(parents=True, exist_ok=True)

    _pool = WorkerPool(_config, _db)
    await _pool.start()

    yield

    await _pool.stop()
    await _db.close()
    log.info("Shutdown complete")


app = FastAPI(
    title="Compound",
    docs_url=None,  # Disable Swagger UI in production
    redoc_url=None,
    lifespan=lifespan,
)


# --- Auth Middleware ---

def _check_basic_auth(request: Request) -> bool:
    """Validate HTTP Basic auth credentials using constant-time comparison."""
    import base64

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, password = decoded.split(":", 1)
    except Exception:
        return False

    user_ok = secrets.compare_digest(user.encode(), _config.auth_user.encode())
    pass_ok = secrets.compare_digest(password.encode(), _config.auth_pass.encode())
    return user_ok and pass_ok


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Health endpoint is public
    if request.url.path == "/health":
        return await call_next(request)

    if not _check_basic_auth(request):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Compound"'},
            content="Unauthorized",
        )

    return await call_next(request)


# --- Health ---

@app.get("/health")
async def health():
    return {"status": "ok"}


# --- Repos ---

class RepoCreate(BaseModel):
    github_url: str
    default_branch: str = "main"

    @field_validator("github_url")
    @classmethod
    def clean_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("Must be an HTTPS URL")
        return v

    @field_validator("default_branch")
    @classmethod
    def clean_branch(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 100:
            raise ValueError("Invalid branch name")
        return v


@app.get("/api/repos")
async def list_repos():
    return await _db.list_repos()


@app.post("/api/repos", status_code=201)
async def add_repo(body: RepoCreate):
    url = validate_github_url(body.github_url, _config)
    name = repo_name_from_url(url)
    local_path = str(_config.repos_dir / name.replace("/", "_"))

    try:
        repo_id = await _db.add_repo(
            name=name,
            github_url=url,
            local_path=local_path,
            default_branch=body.default_branch,
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, "Repo already exists")
        raise

    return {"id": repo_id, "name": name}


# --- Tasks ---

class TaskCreate(BaseModel):
    repo_id: int
    prompt: str
    model: str = "sonnet"
    max_budget_usd: float = Field(default=5.0, gt=0)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Prompt cannot be empty")
        if len(v) > 50_000:
            raise ValueError("Prompt exceeds 50,000 character limit")
        return v

    @field_validator("model")
    @classmethod
    def validate_model(cls, v: str) -> str:
        allowed = ("sonnet", "opus", "haiku")
        if v not in allowed:
            raise ValueError(f"Model must be one of {allowed}")
        return v


@app.get("/api/tasks")
async def list_tasks(status: str | None = None, limit: int = 100):
    if limit > 500:
        limit = 500
    return await _db.list_tasks(status=status, limit=limit)


@app.post("/api/tasks", status_code=201)
async def create_task(body: TaskCreate):
    # Verify repo exists
    repo = await _db.get_repo(body.repo_id)
    if not repo:
        raise HTTPException(404, "Repo not found")

    budget = min(body.max_budget_usd, _config.max_budget_limit_usd)

    task_id = await _db.create_task(
        repo_id=body.repo_id,
        prompt=body.prompt,
        model=body.model,
        max_budget_usd=budget,
    )
    await _db.add_log(task_id, f"Task queued (model={body.model}, budget=${budget:.2f})")

    return {"id": task_id, "status": "queued"}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: int):
    task = await _db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    logs = await _db.get_logs(task_id, limit=200)
    return {**task, "logs": logs}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    ok = await _pool.cancel_task(task_id)
    if not ok:
        raise HTTPException(400, "Cannot cancel this task")
    return {"status": "cancelled"}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int):
    task = await _db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] not in ("failed", "cancelled"):
        raise HTTPException(400, "Can only retry failed or cancelled tasks")

    new_id = await _db.create_task(
        repo_id=task["repo_id"],
        prompt=task["prompt"],
        model=task["model"],
        max_budget_usd=task["max_budget_usd"],
    )
    await _db.add_log(new_id, f"Retry of task #{task_id}")

    return {"id": new_id, "status": "queued"}


# --- Status ---

@app.get("/api/status")
async def pool_status():
    active = await _db.count_active()
    return {
        "active_workers": active,
        "max_workers": _pool.max_workers,
        "queued": len(await _db.list_tasks(status="queued")),
    }


# --- Static files (dashboard) ---

static_dir = Path(__file__).parent.parent / "static"


@app.get("/")
async def dashboard():
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return Response("Dashboard not found", status_code=404)


app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def main():
    import uvicorn

    config = load_config()
    uvicorn.run(
        "src.server:app",
        host=config.host,
        port=config.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
