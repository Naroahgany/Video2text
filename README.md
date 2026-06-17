# B站视频转文字

一个面向个人使用和开源自部署的 B站视频转文字小型 Workflow / 小型 Agent 项目。当前处于阶段 1：工程骨架与部署基础，已建立 FastAPI 后端、原生 HTML/CSS/JS 前端、Docker 和 Render 部署结构。

## 当前状态

- 后端：Python FastAPI。
- 前端：原生 HTML/CSS/JS。
- 部署：Docker + Render Blueprint。
- 数据：后续阶段将把模型配置和历史记录保存在浏览器 IndexedDB，后端不做长期保存。
- 业务功能：B站解析、字幕处理、音频处理、模型调用、历史记录将在后续阶段逐步实现。

## 功能规划

- 粘贴 B站分享文本、视频网址或 BV 号。
- 获取视频信息、字幕和音频。
- 清理字幕、转换 MP3、长音频切片。
- 调用两个 OpenAI-compatible 模型生成最终 Markdown 文稿。
- 在前端保存历史记录、导出、导入和搜索。

## 本地运行

需要 Python 3.12+，并确保本机已安装 FFmpeg。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

健康检查：

```text
http://127.0.0.1:8000/api/health
```

## Docker 运行

```powershell
docker build -t bilibili-transcription-workflow .
docker run --rm -p 8000:8000 bilibili-transcription-workflow
```

Docker 镜像会安装 FFmpeg，并通过 `backend/requirements.txt` 安装 FastAPI、Uvicorn、yt-dlp 和 httpx。

如果本地网络访问 Debian 或 PyPI 较慢，可以在本地构建时临时指定镜像源：

```powershell
docker build --progress=plain `
  --build-arg APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian `
  --build-arg APT_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security `
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple `
  -t bilibili-transcription-workflow .
```

构建成功后再运行容器：

```powershell
docker run --rm -p 8000:8000 bilibili-transcription-workflow
```

## Render 部署

1. Fork 本仓库到自己的 GitHub 账号。
2. 在 Render 中使用 Blueprint 或 Deploy to Render 按钮创建服务。
3. Render 会根据 `render.yaml` 使用 Dockerfile 自动构建。
4. 免费层级会休眠，恢复访问时可能有冷启动。
5. 后端本地文件只用于任务运行期间的临时文件，不用于保存历史数据。

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Naroahgany/Video2text)

按钮已指向当前 GitHub 仓库 `Naroahgany/Video2text`。

## 环境变量

示例见 `.env.example`。当前阶段没有必须配置的服务端密钥。

后续模型 API Key 将由用户在浏览器前端填写并保存在 IndexedDB 中，任务开始时临时发送给后端。不要把 API Key 写入 `.env`、日志或仓库文件。

## 常见错误排查

- `FFmpeg 不可用`：确认本机或 Docker 镜像中已安装 FFmpeg。
- `端口被占用`：修改启动命令中的 `--port`，或设置 Render 的 `PORT`。
- `前端健康检查失败`：确认后端服务已启动，并访问 `/api/health`。
- `apt-get update` 或 `apt-get install` 失败：通常是本地网络无法访问 Debian 源。使用上文带 `APT_MIRROR` 和 `APT_SECURITY_MIRROR` 的 Docker 构建命令重试。
- `docker run` 提示找不到 `bilibili-transcription-workflow`：说明镜像还没有构建成功。先确保 `docker build` 成功，再运行 `docker run`。
- `yt-dlp 解析失败`：后续阶段接入真实 B站处理后，请先升级 yt-dlp 再复查视频权限和链接格式。

## 数据隐私

- 后端不保存用户 API Key。
- 后端不把 API Key 写入日志或响应。
- 后续任务产生的音频和中间文本只在当前任务期间临时处理。
- 历史记录和模型配置将保存在用户浏览器 IndexedDB 中。
- 清空浏览器数据可能删除历史记录，迁移浏览器或电脑前需要先导出数据再导入。

## 使用边界

本项目只适合处理用户有权处理的视频内容，用于个人学习整理、研究或已获授权的场景。MVP 不支持需要登录权限、付费或受限的视频内容。长视频或大量任务建议自行升级部署资源。
