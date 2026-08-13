import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse
import ipaddress

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .local_media import LocalMediaValidationError
from .bilibili_cookie import validate_bilibili_cookie_header
from .browser_profile import (
    consume_cookie_extract_session,
    extract_simplified_cookie_header_from_profile,
    open_login_window,
    profile_status,
    validate_cookie_extract_session,
)
from .models import (
    BilibiliCookieResponse,
    BilibiliCookieValidateRequest,
    BilibiliCookieValidateResponse,
    BilibiliProfileCookieExtractRequest,
    BilibiliProfileStatusResponse,
    ModelListRequest,
    ModelListResponse,
    RefineRetryRequest,
    TaskCreateRequest,
    TaskStatusResponse,
    TranscriptionRetryRequest,
)
from .task_manager import TaskManager, TaskNotFoundError


ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_SRC_DIR = FRONTEND_DIR / "src"
task_manager = TaskManager()
CLEANUP_SWEEP_INTERVAL_SECONDS = 30


async def _cleanup_abandoned_tasks() -> None:
    while True:
        await asyncio.sleep(CLEANUP_SWEEP_INTERVAL_SECONDS)
        await task_manager.sweep_abandoned_tasks()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task_manager.cleanup_stale_temp_dirs()
    cleanup_task = asyncio.create_task(_cleanup_abandoned_tasks())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await task_manager.shutdown()


class NoStoreStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response

app = FastAPI(
    title="B站视频转文字",
    version="0.1.0",
    description="B站视频转文字Workflow / Agent MVP skeleton.",
    lifespan=lifespan,
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


@app.post("/api/tasks/local-upload", response_model=TaskStatusResponse)
async def create_local_upload_task(
    task_request: str = Form(...),
    file: UploadFile = File(...),
) -> TaskStatusResponse:
    try:
        request = TaskCreateRequest.model_validate_json(task_request)
    except ValidationError as exc:
        await file.close()
        raise HTTPException(status_code=422, detail="本地上传任务参数无效") from exc

    try:
        return await task_manager.create_local_media_task(request, file)
    except LocalMediaValidationError as exc:
        await file.close()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        await file.close()
        raise HTTPException(
            status_code=500,
            detail="保存本地上传文件失败，请检查临时目录空间和写入权限。",
        ) from exc


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


@app.post("/api/tasks/{task_id}/retry-transcription", response_model=TaskStatusResponse)
async def retry_transcription(task_id: str, request: TranscriptionRetryRequest) -> TaskStatusResponse:
    try:
        return await task_manager.retry_transcription(task_id, request)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/tasks/{task_id}/retry-refine", response_model=TaskStatusResponse)
async def retry_refine(task_id: str, request: RefineRetryRequest) -> TaskStatusResponse:
    try:
        return await task_manager.retry_refine(task_id, request)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@app.post("/api/bilibili/profile/open-login", response_model=BilibiliProfileStatusResponse)
async def open_bilibili_profile_login() -> BilibiliProfileStatusResponse:
    try:
        payload = await asyncio.to_thread(open_login_window)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return BilibiliProfileStatusResponse(available=True, **payload)


@app.get("/api/bilibili/profile/status", response_model=BilibiliProfileStatusResponse)
async def get_bilibili_profile_status() -> BilibiliProfileStatusResponse:
    return BilibiliProfileStatusResponse(**profile_status())


@app.post("/api/bilibili/profile/extract-cookie", response_model=BilibiliCookieResponse)
async def extract_bilibili_profile_cookie(request: BilibiliProfileCookieExtractRequest) -> BilibiliCookieResponse:
    if not validate_cookie_extract_session(request.session_token):
        raise HTTPException(status_code=403, detail="Cookie提取会话已失效，请重新打开本地B站登录窗口。")

    try:
        payload = extract_simplified_cookie_header_from_profile(request.session_token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    consume_cookie_extract_session(request.session_token)
    return BilibiliCookieResponse(**payload)


@app.post("/api/bilibili/cookie/validate", response_model=BilibiliCookieValidateResponse)
async def validate_bilibili_cookie(request: BilibiliCookieValidateRequest) -> BilibiliCookieValidateResponse:
    payload = await validate_bilibili_cookie_header(request.cookie_header)
    return BilibiliCookieValidateResponse(**payload)


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
            model_id = item.get("id") or item.get("name") or item.get("model")
        else:
            model_id = None
        if isinstance(model_id, str) and model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/")
        if isinstance(model_id, str) and model_id and model_id not in seen:
            models.append(model_id)
            seen.add(model_id)

    return models


def _model_list_base_root(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    for suffix in ("/openai", "/v1beta", "/v1"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return value.rstrip("/")


def _model_list_endpoints(base_url: str, provider: str) -> list[str]:
    base = base_url.strip().rstrip("/")
    provider_value = (provider or "").strip()
    if provider_value.startswith("aistudio_to_api_gemini"):
        root = _model_list_base_root(base)
        endpoints = [f"{root}/v1beta/models"]
    else:
        endpoints = [f"{base}/models"]

    deduped: list[str] = []
    for endpoint in endpoints:
        if endpoint not in deduped:
            deduped.append(endpoint)
    return deduped


def _should_trust_env_for_url(url: str) -> bool:
    """Avoid sending local model list requests through system HTTP proxies."""

    hostname = (urlparse(url.strip()).hostname or "").lower()
    if hostname in {"localhost", "0.0.0.0"}:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_private or address.is_link_local)


@app.post("/api/models/list", response_model=ModelListResponse)
async def list_models(request: ModelListRequest) -> ModelListResponse:
    base_url = request.base_url.strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="API Base URL必须以http:// 或https:// 开头")

    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            trust_env=_should_trust_env_for_url(base_url),
        ) as client:
            last_status: int | None = None
            last_detail = "模型列表为空或格式无法识别"
            for endpoint in _model_list_endpoints(base_url, request.provider):
                response = await client.get(
                    endpoint,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {request.api_key}",
                    },
                )

                if response.status_code in {401, 403}:
                    raise HTTPException(status_code=response.status_code, detail="API Key无效或无权限")
                if response.status_code >= 400:
                    last_status = response.status_code
                    last_detail = f"模型列表获取失败：HTTP {response.status_code}"
                    continue

                try:
                    payload = response.json()
                except ValueError as exc:
                    raise HTTPException(status_code=502, detail="模型列表响应不是有效JSON") from exc

                models = _normalize_model_ids(payload)
                if models:
                    return ModelListResponse(models=models)

            if last_status is not None:
                raise HTTPException(status_code=502, detail=last_detail)
            raise HTTPException(status_code=502, detail=last_detail)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="模型列表获取超时") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="模型列表获取失败，请检查API Base URL") from exc


if FRONTEND_SRC_DIR.exists():
    app.mount("/src", NoStoreStaticFiles(directory=FRONTEND_SRC_DIR), name="frontend-src")


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    response = FileResponse(FRONTEND_DIR / "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response
