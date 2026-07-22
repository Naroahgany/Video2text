$ErrorActionPreference = "Stop"

Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
$ProjectRoot = (Get-Location).Path
$RuntimeDir = Join-Path $ProjectRoot "runtime"
$DownloadsDir = Join-Path $RuntimeDir "downloads"
$ToolsDir = Join-Path $RuntimeDir "tools"
$BrowserCacheDir = Join-Path $RuntimeDir "browser-cache"
$DataDir = Join-Path $ProjectRoot "data"
$LogsDir = Join-Path $ProjectRoot "logs"
$TempDir = Join-Path $DataDir "temp"
$VenvDir = Join-Path $RuntimeDir ".venv"
$DependencyStamp = Join-Path $RuntimeDir ".dependencies-installed"
$RequirementsFile = Join-Path $ProjectRoot "backend\requirements.txt"
$PythonInstaller = Join-Path $DownloadsDir "python-3.12.8-amd64.exe"
$PythonInstallDir = Join-Path $ToolsDir "python-3.12"
$FfmpegDir = Join-Path $ToolsDir "ffmpeg"
$WindowsPythonUrl = if ($env:VIDEO2TEXT_PYTHON_URL) { $env:VIDEO2TEXT_PYTHON_URL } else { "https://mirrors.tuna.tsinghua.edu.cn/python/3.12.8/python-3.12.8-amd64.exe" }
$DefaultPipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
$DefaultPlaywrightDownloadHost = "https://npmmirror.com/mirrors/playwright"

function Write-Step($Message) {
  Write-Host "[INFO] $Message"
}

function Write-Fail($Message, $Suggestion) {
  Write-Host "[ERROR] $Message" -ForegroundColor Red
  Write-Host "[NEXT] $Suggestion" -ForegroundColor Yellow
}

function Write-DownloadNotice($Name, $Url, $Target) {
  Write-Host ""
  Write-Host "【环境准备】未检测到可用的 $Name，启动脚本将自动下载并准备它。" -ForegroundColor Cyan
  Write-Host "下载来源：$Url" -ForegroundColor Cyan
  Write-Host "保存位置：$Target" -ForegroundColor Cyan
  Write-Host "请保持网络连接并等待下载完成，后续再次启动会优先复用已安装的本地文件。" -ForegroundColor Cyan
  Write-Host ""
}

function Ensure-Directory($Path) {
  if (-not (Test-Path $Path)) {
    New-Item -ItemType Directory -Path $Path | Out-Null
  }
}

function Use-DomesticInstallDefaults {
  if (-not $env:PIP_INDEX_URL) {
    $env:PIP_INDEX_URL = $DefaultPipIndexUrl
    Write-Step "Using default PyPI mirror for mainland direct network: $DefaultPipIndexUrl"
  } else {
    Write-Step "Using user configured PyPI index: $env:PIP_INDEX_URL"
  }

  if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowserCacheDir
    Write-Step "Using project Playwright browser cache: $BrowserCacheDir"
  } else {
    Write-Step "Using user configured Playwright browser cache: $env:PLAYWRIGHT_BROWSERS_PATH"
  }

  if (-not $env:PLAYWRIGHT_DOWNLOAD_HOST) {
    $env:PLAYWRIGHT_DOWNLOAD_HOST = $DefaultPlaywrightDownloadHost
    Write-Step "Using default Playwright mirror for mainland direct network: $DefaultPlaywrightDownloadHost"
  } else {
    Write-Step "Using user configured Playwright browser mirror: $env:PLAYWRIGHT_DOWNLOAD_HOST"
  }
}

function Test-PythonCommand($Command) {
  try {
    $VersionOutput = & $Command --version 2>&1
    if ($LASTEXITCODE -eq 0 -and "$VersionOutput" -match "Python 3\.(1[0-9]|[2-9][0-9])\.") {
      return $true
    }
  } catch {
    return $false
  }
  return $false
}

function Get-SystemPython {
  if (Get-Command python -ErrorAction SilentlyContinue) {
    if (Test-PythonCommand "python") {
      return "python"
    }
  }

  if (Get-Command py -ErrorAction SilentlyContinue) {
    try {
      $VersionOutput = & py -3 --version 2>&1
      if ($LASTEXITCODE -eq 0 -and "$VersionOutput" -match "Python 3\.(1[0-9]|[2-9][0-9])\.") {
        return "py -3"
      }
    } catch {}
  }

  return $null
}

function Ensure-EmbeddedPython {
  $PythonExe = Join-Path $PythonInstallDir "python.exe"
  if (Test-Path $PythonExe) {
    return $PythonExe
  }

  Write-DownloadNotice "Python 3.12" $WindowsPythonUrl $PythonInstaller
  Ensure-Directory $DownloadsDir
  Ensure-Directory $PythonInstallDir
  if (-not (Test-Path $PythonInstaller)) {
    try {
      Write-Step "Downloading Python from domestic mirror: $WindowsPythonUrl"
      Invoke-WebRequest -Uri $WindowsPythonUrl -OutFile $PythonInstaller
    } catch {
      if (Test-Path $PythonInstaller) {
        Remove-Item -LiteralPath $PythonInstaller -Force -ErrorAction SilentlyContinue
      }
      throw "Python installer could not be downloaded from the configured domestic mirror: $WindowsPythonUrl"
    }
  } else {
    Write-Step "Reusing downloaded Python installer: $PythonInstaller"
  }
  Write-Step "Installing Python to runtime/tools/python-3.12."
  Start-Process -FilePath $PythonInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_test=0 TargetDir=`"$PythonInstallDir`"" -Wait

  if (-not (Test-Path $PythonExe)) {
    throw "Python installer finished but python.exe was not found."
  }
  return $PythonExe
}

function Get-PythonCommand {
  Write-Step "Checking Python."
  $SystemPython = Get-SystemPython
  if ($SystemPython) {
    Write-Step "Using existing Python: $SystemPython"
    return $SystemPython
  }

  try {
    $PythonExe = Ensure-EmbeddedPython
    Write-Step "Using local Python runtime: $PythonExe"
    return "`"$PythonExe`""
  } catch {
    Write-Fail "Unable to prepare Python automatically." "Check access to the configured domestic Python mirror, or install Python 3.12 manually, then run start-windows.bat again."
    throw
  }
}

function Ensure-Ffmpeg($VenvPython) {
  Write-Step "Checking FFmpeg."
  if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Step "Using system FFmpeg."
    return
  }

  $LocalFfmpeg = Get-ChildItem -Path $FfmpegDir -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($LocalFfmpeg) {
    $env:PATH = "$($LocalFfmpeg.DirectoryName);$env:PATH"
    Write-Step "Using local FFmpeg: $($LocalFfmpeg.FullName)"
    return
  }

  if (Use-ImageioFfmpeg $VenvPython) {
    return
  }

  Write-Fail "Unable to prepare FFmpeg automatically." "Check access to the configured PyPI domestic mirror, or install FFmpeg manually, then run start-windows.bat again."
  throw "imageio-ffmpeg could not provide a usable FFmpeg executable."
}

function Use-ImageioFfmpeg($VenvPython) {
  try {
    Write-Step "Preparing FFmpeg from imageio-ffmpeg via PyPI mirror."
    & $VenvPython -m pip install "imageio-ffmpeg>=0.5.1,<1.0.0"
    if ($LASTEXITCODE -ne 0) {
      throw "imageio-ffmpeg installation failed."
    }
    $EncodedImageioFfmpeg = (& $VenvPython -c "import base64, imageio_ffmpeg; print(base64.b64encode(imageio_ffmpeg.get_ffmpeg_exe().encode('utf-8')).decode('ascii'))" 2>$null | Select-Object -First 1).Trim()
    $ImageioFfmpeg = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($EncodedImageioFfmpeg))
    if ($ImageioFfmpeg -and (Test-Path $ImageioFfmpeg)) {
      $ImageioDir = Split-Path -Parent $ImageioFfmpeg
      $env:PATH = "$ImageioDir;$env:PATH"
      Write-Step "Using imageio-ffmpeg binary: $ImageioFfmpeg"
      return $true
    }
    Write-Step "imageio-ffmpeg did not return a usable ffmpeg executable."
  } catch {
    Write-Step "imageio-ffmpeg preparation failed: $($_.Exception.Message)"
  }
  return $false
}

function Ensure-Venv($PythonCommand) {
  Write-Step "Creating or reusing local virtual environment."
  $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
  if (-not (Test-Path $VenvPython)) {
    Invoke-Expression "$PythonCommand -m venv `"$VenvDir`""
  }
  return $VenvPython
}

function Test-BackendDependencies($VenvPython) {
  try {
    & $VenvPython -c "import fastapi, uvicorn, yt_dlp, httpx, curl_cffi, playwright.sync_api, imageio_ffmpeg" 2>$null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Install-Dependencies($VenvPython) {
  $RequirementsStamp = ""
  if (Test-Path $RequirementsFile) {
    $RequirementsStamp = (Get-FileHash $RequirementsFile -Algorithm SHA256).Hash
  }
  $CurrentStamp = ""
  if (Test-Path $DependencyStamp) {
    $CurrentStamp = Get-Content $DependencyStamp -Raw
  }

  if ($CurrentStamp.Trim() -eq $RequirementsStamp -and $RequirementsStamp -and (Test-BackendDependencies $VenvPython)) {
    Write-Step "Backend dependencies are already installed; skipping pip install."
    return
  }

  Write-Step "Installing backend dependencies. First launch or dependency changes may download packages."
  & $VenvPython -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    throw "pip upgrade failed."
  }
  & $VenvPython -m pip install -r $RequirementsFile
  if ($LASTEXITCODE -ne 0) {
    throw "Backend dependency installation failed."
  }
  Set-Content -Path $DependencyStamp -Value $RequirementsStamp
}

function Test-PlaywrightChromiumAvailable($VenvPython) {
  try {
    & $VenvPython -c "from pathlib import Path; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); executable = p.chromium.executable_path; p.stop(); raise SystemExit(0 if Path(executable).is_file() else 1)" 2>$null
    return $LASTEXITCODE -eq 0
  } catch {
    return $false
  }
}

function Ensure-PlaywrightChromium($VenvPython) {
  Write-Step "Checking Playwright Chromium."
  if (Test-PlaywrightChromiumAvailable $VenvPython) {
    Write-Step "Using Playwright Chromium from: $env:PLAYWRIGHT_BROWSERS_PATH"
    return
  }

  $DownloadSource = $env:PLAYWRIGHT_DOWNLOAD_HOST
  Write-DownloadNotice "Playwright Chromium" $DownloadSource $env:PLAYWRIGHT_BROWSERS_PATH
  & $VenvPython -m playwright install chromium
  if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed."
  }

  if (-not (Test-PlaywrightChromiumAvailable $VenvPython)) {
    throw "Playwright Chromium installation finished but the browser executable was not found."
  }
  Write-Step "Playwright Chromium is ready in: $env:PLAYWRIGHT_BROWSERS_PATH"
}

function Test-PortAvailable($Port) {
  $Listener = $null
  try {
    $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), $Port)
    $Listener.Start()
    return $true
  } catch {
    return $false
  } finally {
    if ($Listener) {
      $Listener.Stop()
    }
  }
}

function Get-AvailablePort {
  for ($Port = 8000; $Port -le 8099; $Port++) {
    if (Test-PortAvailable $Port) {
      return $Port
    }
  }
  throw "No available port found between 8000 and 8099."
}

function Start-BrowserAfterHealthCheck($Url) {
  $HealthUrl = "$Url/api/health"
  Start-Job -ArgumentList $Url, $HealthUrl -ScriptBlock {
    param($OpenUrl, $ProbeUrl)

    for ($Attempt = 0; $Attempt -lt 240; $Attempt++) {
      try {
        $Response = Invoke-WebRequest -Uri $ProbeUrl -UseBasicParsing -TimeoutSec 1
        if ($Response.StatusCode -eq 200) {
          Start-Process $OpenUrl
          return
        }
      } catch {}

      Start-Sleep -Milliseconds 500
    }
  } | Out-Null
}

try {
  Write-Host "Bilibili Video to Text - local desktop startup"
  Ensure-Directory $RuntimeDir
  Ensure-Directory $DownloadsDir
  Ensure-Directory $ToolsDir
  Ensure-Directory $DataDir
  Ensure-Directory $LogsDir
  Ensure-Directory $TempDir
  Use-DomesticInstallDefaults

  $PythonCommand = Get-PythonCommand
  $VenvPython = Ensure-Venv $PythonCommand
  Install-Dependencies $VenvPython
  Ensure-PlaywrightChromium $VenvPython
  Ensure-Ffmpeg $VenvPython

  $Port = Get-AvailablePort
  $Url = "http://127.0.0.1:$Port"
  if ($Port -ne 8000) {
    Write-Step "Port 8000 is busy. Using port $Port instead."
  }

  $env:PORT = "$Port"
  $env:TEMP_DIR = $TempDir
  $env:GLOBAL_TASK_CONCURRENCY = "1"
  $env:AUDIO_REQUEST_CONCURRENCY = "2"

  Write-Step "Starting FastAPI service. Keep this window open while using the app."
  Write-Step "Browser will open after local health check passes: $Url"
  Start-BrowserAfterHealthCheck $Url
  & $VenvPython -m uvicorn backend.app.main:app --host 127.0.0.1 --port $Port
} catch {
  Write-Fail "Startup stopped: $($_.Exception.Message)" "Check network access, Python/FFmpeg/Playwright installation, mirror settings, and whether another app is using ports 8000-8099."
  exit 1
}
