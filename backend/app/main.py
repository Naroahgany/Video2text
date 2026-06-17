from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_SRC_DIR = FRONTEND_DIR / "src"

app = FastAPI(
    title="B站视频转文字",
    version="0.1.0",
    description="B站视频转文字 Workflow / Agent MVP skeleton.",
)


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "bilibili-transcription-workflow",
        "frontend": "native-html-css-js",
    }


if FRONTEND_SRC_DIR.exists():
    app.mount("/src", StaticFiles(directory=FRONTEND_SRC_DIR), name="frontend-src")


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
