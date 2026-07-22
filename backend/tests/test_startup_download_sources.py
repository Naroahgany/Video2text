from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_STARTUP = (PROJECT_ROOT / "scripts" / "start-local.ps1").read_text(encoding="utf-8")
MACOS_STARTUP = (PROJECT_ROOT / "scripts" / "start-local.sh").read_text(encoding="utf-8")
REQUIREMENTS = (PROJECT_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")


def test_first_run_downloads_default_to_mainland_mirrors():
    for script in (WINDOWS_STARTUP, MACOS_STARTUP):
        assert "https://pypi.tuna.tsinghua.edu.cn/simple" in script
        assert "https://npmmirror.com/mirrors/playwright" in script
        assert "PLAYWRIGHT_DOWNLOAD_HOST" in script

    assert "https://mirrors.tuna.tsinghua.edu.cn/python/3.12.8/python-3.12.8-amd64.exe" in WINDOWS_STARTUP
    assert "https://mirrors.tuna.tsinghua.edu.cn/python/3.12.8/python-3.12.8-macos11.pkg" in MACOS_STARTUP


def test_startup_scripts_do_not_fall_back_to_foreign_download_hosts():
    combined = WINDOWS_STARTUP + MACOS_STARTUP
    foreign_hosts = (
        "python.org/ftp",
        "evermeet.cx",
        "gyan.dev",
        "cdn.playwright.dev",
        "playwright.azureedge.net",
    )

    for host in foreign_hosts:
        assert host not in combined

    assert "VIDEO2TEXT_FFMPEG_URL" not in combined


def test_playwright_and_ffmpeg_downloads_are_reproducible():
    assert "playwright==1.61.0" in REQUIREMENTS
    assert "imageio-ffmpeg>=0.5.1,<1.0.0" in REQUIREMENTS
    assert 'pip install "imageio-ffmpeg>=0.5.1,<1.0.0"' in WINDOWS_STARTUP
    assert "pip install 'imageio-ffmpeg>=0.5.1,<1.0.0'" in MACOS_STARTUP
    assert WINDOWS_STARTUP.index("Install-Dependencies $VenvPython") < WINDOWS_STARTUP.index("Ensure-Ffmpeg $VenvPython")
    assert MACOS_STARTUP.index('install_dependencies "$VENV_PYTHON"') < MACOS_STARTUP.index('ensure_ffmpeg "$VENV_PYTHON"')