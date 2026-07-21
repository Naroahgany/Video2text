# B站视频转文字

一个面向个人使用的 B站视频转文字小型 Workflow / 小型 Agent 项目。项目主线是 Windows / Mac 本地桌面 zip 启动版：用户下载仓库 zip 后，通过启动脚本在本机准备环境、启动本地服务，并在浏览器中打开本地网页使用。

## 当前状态

- 后端：Python FastAPI。
- 前端：原生 HTML/CSS/JS。
- 运行：本地 zip / 桌面启动版。
- 数据：模型配置、历史记录、最终结果和精简 B站 Cookie 保存在浏览器 IndexedDB，后端不做长期保存。
- 主流程：阶段 4.3 精简 Cookie 字幕路径、阶段 5A 音频下载 / MP3 转换 / 切片、阶段 6 第一模型音频转文字、阶段 7 第二模型文稿优化与 Markdown 输出已经接入。
- 当前 MVP 收尾：已补齐历史保存、搜索、全量导出 / 导入、清空历史、中间结果 TXT 下载，以及错误处理、安全隐私和发布说明。

## 功能说明

- 粘贴 B站分享文本、视频网址或 BV 号。
- 获取视频信息、字幕和音频。
- 清理字幕、转换 MP3、长音频切片。
- 调用第一模型把 MP3 或 MP3 切片转成文字。
- 调用第二模型把清理后的 B站字幕和 AI 音频转写稿合并成最终 Markdown 文稿。
- 展示最终 Markdown，并支持下载 .md 文件。
- 展开查看中间结果：清理后的 B站字幕、AI 音频转文字稿和完整日志。
- 分别下载字幕 TXT、AI 转写 TXT 和日志 TXT。
- 在前端 IndexedDB 保存历史记录，支持按标题、正文、字幕、转写稿或错误信息搜索。
- 支持全量导出 / 导入 IndexedDB 数据，以及清空历史记录。
- 多P视频当前按 MVP 处理当前分P，并在历史记录中保留多P子任务基础结构；批量多P逐个处理仍属于后续增强。

## 下载 zip 本地运行

普通用户可以在 GitHub 仓库页面点击 `Code` → `Download ZIP`，解压到桌面或其他本机目录后，通过根目录启动脚本运行本地服务。启动脚本会自动完成：

1. 检测本机 Python。
2. 检测 FFmpeg。
3. 创建或复用 `runtime/.venv` 虚拟环境。
4. 安装 `backend/requirements.txt` 中的后端依赖。
5. 检测 `8000` 端口；如果被占用，自动在 `8000-8099` 中选择可用端口。
6. 启动 FastAPI 服务，并打开默认浏览器访问本地页面。

启动脚本默认按国内直连网络准备 Python 包依赖：Python 包安装默认使用清华 PyPI 镜像。如需改用其他源，可在启动前设置 `PIP_INDEX_URL`、`VIDEO2TEXT_PYTHON_URL` 或 `VIDEO2TEXT_FFMPEG_URL`。Playwright Chromium 不再作为启动必经下载项；只有打开本地 B站登录窗口时才需要浏览器运行时，如果届时提示缺少浏览器组件，再按提示安装或设置已验证可用的 `PLAYWRIGHT_DOWNLOAD_HOST`。

推荐打开地址：

```text
http://127.0.0.1:8000
```

如果脚本使用备用端口，浏览器会打开对应地址，例如 `http://127.0.0.1:8001`。

健康检查：

```text
http://127.0.0.1:8000/api/health
```

## Windows 本地启动

1. 解压项目 zip。
2. 双击根目录的 `start-windows.bat`。
3. 保持弹出的控制台窗口不要关闭。
4. 等待脚本显示检测 Python、检测 FFmpeg、安装依赖、启动服务和打开浏览器的状态。

Windows 脚本策略：

- 如果本机已有可用 Python 3，则直接复用，不强制安装固定小版本。
- 如果没有 Python，会优先从清华 Python 镜像下载 `Python 3.12.8` Windows amd64 安装包，失败后再尝试官方地址。
- 如果没有 FFmpeg，会优先通过清华 PyPI 镜像安装 `imageio-ffmpeg` 并使用其中的 `ffmpeg.exe`；如果该路径失败，才尝试旧的 FFmpeg zip 下载兜底。
- Python 依赖默认从清华 PyPI 镜像安装；Playwright Chromium 不在启动时强制下载，避免浏览器镜像不可用时阻塞主服务。
- 如果自动下载失败，请按控制台提示手动安装 Python 或 FFmpeg 后重新双击脚本。

## Mac 本地启动

1. 解压项目 zip。
2. 首次运行时，如果系统提示脚本没有执行权限，请在终端进入项目目录后运行：

```bash
chmod +x start-mac.command scripts/start-local.sh
```

3. 双击根目录的 `start-mac.command`。
4. 保持弹出的终端窗口不要关闭。
5. 等待脚本显示检测 Python、检测 FFmpeg、安装依赖、启动服务和打开浏览器的状态。

Mac 脚本策略：

- 如果本机已有可用 Python 3，则直接复用，不强制安装固定小版本。
- 如果没有 Python，会尝试从 Python 官方下载地址下载 `Python 3.12.8` macOS universal2 安装包，并打开官方安装器；安装完成后按回车继续。
- 如果没有 FFmpeg，会尝试下载 macOS FFmpeg zip 并解压到 `runtime/tools/ffmpeg`。
- Python 依赖默认从清华 PyPI 镜像安装；Playwright Chromium 不在启动时强制下载，避免浏览器镜像不可用时阻塞主服务。
- 如果自动下载失败，请按控制台提示手动安装 Python 或 FFmpeg 后重新双击脚本。

## GitHub 上传 / 打包注意

上传或推送 GitHub 前，请先检查 `.gitignore` 和 Git 暂存区。公开项目本体应包含业务代码、启动脚本、README、依赖声明、示例配置和必要测试；`docs/`、`AGENTS.md` 和根目录研究资料仅供本地开发使用，不上传。也不要上传 `runtime/`、`.venv/`、`data/temp/`、`logs/`、`__pycache__/`、`.claude/`、`.codex/`、`.agents/`、音视频临时文件、IndexedDB 导出 JSON、真实 Cookie 或任何密钥。

可以用以下命令先检查：

```powershell
git status --short --ignored
git ls-files -o --exclude-standard
git check-ignore -v runtime/.venv runtime/tools/ffmpeg runtime/browser-profile data/temp logs debug.log .claude/settings.local.json
```

## 开发方式启动

如果你需要以开发方式启动，可以手动运行：

```powershell
python -m venv runtime\.venv
.\runtime\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

## 环境变量

示例见 `.env.example`。当前阶段没有必须配置的服务端密钥。

后续模型 API Key 将由用户在浏览器前端填写并保存在 IndexedDB 中，任务开始时临时发送给后端。不要把 API Key 写入 `.env`、日志或仓库文件。

## B站访问排错

本地页面会通过新手引导或右上角“设置”管理 B站访问凭据。当前稳定流程是：首次打开时检查 IndexedDB 中是否已有精简 B站 Cookie；没有或经 nav 轻量校验失效时，引导用户打开本地专用 B站登录窗口；用户在该窗口完成扫码或账号登录后，先保持 B站窗口打开，再回到本地页面点击“提取并保存Cookie”；提取成功后程序会关闭该 B站窗口。

程序只保存 6 项精简 Cookie 到 IndexedDB，用于本地个人测试、连续处理多个视频和导出 / 导入迁移。完整 Cookie、localStorage、完整请求头和完整 Profile 路径不会写入日志或返回给前端。

后端会在拿到 aid、cid 后尝试播放器字幕接口、WBI 字幕接口、HTML 初始化数据回退和 yt-dlp 字幕候选路径。音频下载当前默认使用 x/player/playurl 主路径，只下载音频流，不下载完整视频画面。

- HTTP 412 Precondition Failed：通常表示 B站风控、访问前置条件失败或 Cookie 失效，不代表程序整体损坏。请按引导重新打开本地 B站登录窗口并刷新精简 Cookie。
- 本地专用浏览器 Profile：用于首次凭据初始化和 Cookie 刷新。登录完成后请保持 B站窗口打开，再回到本地页面点击“提取并保存Cookie”；提取成功后程序会关闭登录窗口。
- 精简 Cookie：只保留 SESSDATA、bili_jct、DedeUserID、DedeUserID__ckMd5、bili_ticket、bili_ticket_expires，并按固定顺序组成标准 Cookie Header。
- 重新登录 / 刷新 Cookie：如果任务提示 Cookie 失效、HTTP 412、未登录或风控，请进入“设置”或新手引导，重新打开 B站登录窗口登录，并点击“提取并保存Cookie”。
- 清理本地 Profile：关闭本地服务和登录窗口后，删除项目目录下的 runtime/browser-profile/ 即可清理 B站 Profile 登录态；下次刷新 Cookie 时需要重新登录。
- 登录态有效期：同一 B站账号通常可以在多个浏览器或设备同时登录，但登录态有效期由 B站控制，不保证永久有效。
- HTTP 403 Forbidden：通常表示访问被拒绝、权限受限或视频需要登录权限。
- HTTP 404 Not Found：通常表示视频不存在、链接失效或当前网络无法访问。
- B站视频信息 API：后端内部会用 B站 API 获取视频标题、分 P、aid/cid，再请求播放器字幕接口；这不是前端独立访问模式，也不下载视频画面。
- Could not copy Chrome cookie database：通常表示浏览器 Cookie 数据库被占用或本机浏览器限制。浏览器 Cookie 入口当前已从前端下线，后端兼容代码仅作为历史/高级排错保留。
- 浏览器 Cookie / cookies.txt：当前普通用户主流程不需要手动导入；后端兼容代码仅作为历史/高级排错保留。
- 导出 / 导入：当前 MVP 允许 IndexedDB 导出文件包含精简 Cookie、模型配置和历史记录，方便个人测试和迁移；导出文件属于敏感本地文件，不要外传。
- Profile 隐私边界：本地专用浏览器 Profile 只能保存在本机项目运行目录，不得上传第三方；前端 IndexedDB 只保存白名单过滤后的精简 Cookie。
- 视频需要登录、付费或受限：MVP 只面向用户有权处理的个人学习整理场景，不支持绕过付费、会员、私密或受限内容。

## 常见错误排查

- `FFmpeg 不可用`：启动脚本会优先复用系统 FFmpeg，检测不到时会通过清华 PyPI 镜像安装 `imageio-ffmpeg` 提供的 `ffmpeg.exe`；如果仍失败，请手动安装 FFmpeg 后重新运行脚本。
- `端口被占用`：启动脚本会自动从 `8000-8099` 选择可用端口，并打开正确 URL。
- `前端健康检查失败`：确认启动脚本控制台窗口仍在运行，并访问 `/api/health`。
- `依赖安装失败`：检查 Python 版本、网络连接和 `runtime/.venv` 虚拟环境；必要时删除 `runtime/.venv` 后重试。
- `Python 下载失败`：检查网络是否可以访问 `python.org`，或手动安装 Python 3.12 后重新运行脚本。
- `Mac 无法双击运行`：先执行 `chmod +x start-mac.command scripts/start-local.sh`，或在“系统设置”里允许打开该脚本。
- `用户输入无法解析`：请确认输入中包含 B站视频链接、b23.tv 短链或 BV 号。
- `没有检测到字幕`：该视频可能没有 UP 主字幕或自动字幕；页面会提示是否跳过字幕，继续使用 AI 音频转写稿生成最终文稿。
- `字幕获取超时 / 字幕格式无法解析`：通常是网络、B站返回结构变化或字幕文件异常；可稍后重试或刷新精简 Cookie。
- `音频下载失败`：通常与 Cookie 失效、HTTP 403/412、playurl 无音频流或网络中断有关；先刷新精简 Cookie，再换一个公开视频验证。
- `MP3 转换失败 / 音频切片失败`：优先检查 FFmpeg 是否可用、本机磁盘空间是否足够，以及源音频是否下载完整。
- `模型 API Base URL 无效`：Base URL 必须以 http:// 或 https:// 开头；OpenAI-compatible 通常填写到 /v1，AIStudioToAPI Gemini 原生路径填写服务根地址。
- `API Key 无效或无权限`：重新检查前端设置中的 API Key，后端不会保存或回显完整 API Key。
- `模型列表获取失败`：检查 Base URL、API Key、网络代理和服务商模型列表接口是否兼容。
- `模型不支持音频多模态`：第一模型必须能接收 MP3 音频输入；如使用 AIStudioToAPI Gemini，优先选择 Gemini 原生 Files API provider。
- `API 超时 / 限流`：长视频、切片多或服务商限制可能导致请求变慢；稍后重试，或更换模型 / 服务商。
- `第二模型输出格式异常`：第二模型需要在完整输出末尾返回 [finish] 标识；系统会自动续写，仍失败时会给出明确错误。
- `历史导入失败`：确认导入文件是本工具导出的 JSON，且没有被手动破坏；导入会覆盖当前 IndexedDB 中的配置、精简 Cookie 和历史记录。

## 数据隐私

- 后端不保存用户 API Key。
- 后端不把 API Key 写入日志或响应。
- 任务产生的音频和中间文本只在当前任务期间临时处理。
- 启动脚本会把任务临时目录限制在项目内的 `data/temp/`；后端任务完成、失败、取消或 abandoned 后会尝试清理临时目录。
- `runtime/` 存放虚拟环境、下载缓存、本地工具和本地专用浏览器 Profile；`logs/` 预留给本地日志。
- 历史记录、模型配置和精简 B站 Cookie 保存在用户浏览器 IndexedDB 中。
- 历史记录会保存视频标题、原始输入、解析结果、最终 Markdown、清理字幕、AI 音频转写稿、完整日志、错误信息和多P基础子任务结构；不会保存音频文件。
- 全量导出会包含历史记录、模型配置和精简 B站 Cookie，导出文件属于敏感本地文件，不要发给别人或上传到公共平台。
- 清空浏览器数据可能删除历史记录和精简 Cookie，迁移浏览器或电脑前需要先导出数据再导入。
- 日志会自动脱敏 API Key、Authorization 头、Bearer token、Cookie 和常见令牌形态；如果你手动把密钥写进视频标题或正文，仍应谨慎处理导出文件。

## 使用边界

本项目只适合处理用户有权处理的视频内容，用于个人学习整理、研究或已获授权的场景。MVP 不支持需要登录权限、付费或受限的视频内容。长视频或大量任务需要预留足够本机磁盘、CPU、内存和网络时间。
