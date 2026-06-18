from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import ModelListRequest, ModelListResponse, TaskCreateRequest, TaskStatusResponse
from .task_manager import TaskManager, TaskNotFoundError


ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_SRC_DIR = FRONTEND_DIR / "src"
task_manager = TaskManager()

app = FastAPI(
    title="B站视频转文字",
    version="0.1.0",
    description="B站视频转文字 Workflow / Agent MVP skeleton.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(127\.0\.0\.1|localhost):\d+",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "bilibili-transcription-workflow",
        "frontend": "native-html-css-js",
    }


@app.post("/api/tasks", response_model=TaskStatusResponse)
async def create_task(request: TaskCreateRequest) -> TaskStatusResponse:
    return await task_manager.create_task(request)


@app.get("/api/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str) -> TaskStatusResponse:
    try:
        return await task_manager.get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/tasks/{task_id}/cancel", response_model=TaskStatusResponse)
async def cancel_task(task_id: str) -> TaskStatusResponse:
    try:
        return await task_manager.cancel_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


def _normalize_model_ids(payload: object) -> list[str]:
    if isinstance(payload, dict):
        raw_models = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        raw_models = payload
    else:
        raw_models = []

    models: list[str] = []
    seen: set[str] = set()
    for item in raw_models:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id")
        else:
            model_id = None
        if isinstance(model_id, str) and model_id and model_id not in seen:
            models.append(model_id)
            seen.add(model_id)

    return models


@app.post("/api/models/list", response_model=ModelListResponse)
async def list_models(request: ModelListRequest) -> ModelListResponse:
    base_url = request.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="API Base URL 必须以 http:// 或 https:// 开头")

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(
                f"{base_url}/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {request.api_key}",
                },
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="模型列表获取超时") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="模型列表获取失败，请检查 API Base URL") from exc

    if response.status_code in {401, 403}:
        raise HTTPException(status_code=response.status_code, detail="API Key 无效或无权限")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"模型列表获取失败：HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="模型列表响应不是有效 JSON") from exc

    models = _normalize_model_ids(payload)
    if not models:
        raise HTTPException(status_code=502, detail="模型列表为空或格式无法识别")

    return ModelListResponse(models=models)


if FRONTEND_SRC_DIR.exists():
    app.mount("/src", StaticFiles(directory=FRONTEND_SRC_DIR), name="frontend-src")


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
