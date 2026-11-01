from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import re
import shutil
import sys
import time
import subprocess
import tempfile
import zipfile
from io import BytesIO
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, TypedDict, cast
from urllib.parse import urljoin

import streamlit as st
import streamlit.components.v1 as components
import yt_dlp
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


USERNAME_SELECTORS = (
    'input[name="username"]',
    'input[name="account"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="username"]',
    'input[id*="user" i]',
)

PASSWORD_SELECTORS = (
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="current-password"]',
    'input[id*="pass" i]',
)

SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("登入")',
    'button:has-text("Log in")',
    'button:has-text("Sign in")',
)

APP_VERSION = "v2026.06.01.1"
APP_UPDATED_AT = "2026-06-01"
DEPLOY_VERIFY_MIN_COMMIT = "7f71b98"
DEPLOY_MARKER = "build-2026-06-01-01-mac-fallback"

SUBTITLE_EXTENSIONS = {
    ".srt",
    ".vtt",
    ".ass",
    ".ssa",
    ".lrc",
    ".ttml",
    ".sbv",
    ".dfxp",
    ".xml",
}

SubtitleRow = dict[str, str]


class SubtitleDebugReport(TypedDict):
    title: str
    extractor: str
    manual_rows: list[SubtitleRow]
    auto_rows: list[SubtitleRow]
    inspection_error: str


class StatusUI(Protocol):
    def info(self, message: str) -> None: ...


class ProgressUI(Protocol):
    def progress(self, value: float, text: str = "") -> None: ...


playwright_chromium_ready = False
ffmpeg_status_cache: Optional[tuple[bool, str]] = None


def get_ffmpeg_status() -> tuple[bool, str]:
    global ffmpeg_status_cache
    if ffmpeg_status_cache is not None:
        return ffmpeg_status_cache

    executable = shutil.which("ffmpeg")
    if not executable:
        ffmpeg_status_cache = (False, "PATH 中找不到 ffmpeg")
        return ffmpeg_status_cache

    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception as exc:
        ffmpeg_status_cache = (False, f"ffmpeg 無法執行：{exc}")
        return ffmpeg_status_cache

    if result.returncode != 0:
        stderr_text = (result.stderr or "").strip()
        stdout_text = (result.stdout or "").strip()
        details = stderr_text or stdout_text or "unknown error"
        ffmpeg_status_cache = (False, f"ffmpeg 無法使用：{details}")
        return ffmpeg_status_cache

    ffmpeg_status_cache = (True, executable)
    return ffmpeg_status_cache


def fill_first(page: Page, selectors: Iterable[str], value: str) -> Optional[str]:
    for selector in selectors:
        try:
            page.locator(selector).first.fill(value, timeout=1200)
            return selector
        except Exception:
            continue
    return None


def click_first(page: Page, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=1200)
            return selector
        except Exception:
            continue
    return None


def normalize_url(src: str, base_url: str) -> str:
    if src.startswith("//"):
        return f"https:{src}"
    if src.startswith("http://") or src.startswith("https://"):
        return src
    return urljoin(base_url, src)


def find_vimeo_player_url(page: Page, timeout_ms: int = 25000) -> Optional[str]:
    patterns = (
        r"https?://player\.vimeo\.com/video/\d+[^\"'\\\s<]*",
        r"//player\.vimeo\.com/video/\d+[^\"'\\\s<]*",
        r"https?://vimeo\.com/\d+",
    )

    deadline = time.time() + max(timeout_ms, 1000) / 1000.0
    while time.time() < deadline:
        for attr in ("src", "data-src"):
            locator = page.locator(f'iframe[{attr}*="player.vimeo.com/video/"]')
            if locator.count() > 0:
                raw = locator.first.get_attribute(attr)
                if raw:
                    return normalize_url(raw, page.url)

        for frame in page.frames:
            frame_url = (frame.url or "").strip()
            if "player.vimeo.com/video/" in frame_url or re.search(r"https?://vimeo\.com/\d+", frame_url):
                return normalize_url(frame_url, page.url)

        page.wait_for_timeout(1000)

    # Page text can mention unrelated videos, so only search it once no player frame has appeared.
    html_sources: list[str] = []
    try:
        html_sources.append(unescape(page.content()))
    except Exception:
        pass

    for frame in page.frames:
        try:
            html_sources.append(unescape(frame.content()))
        except Exception:
            continue

    for pattern in patterns:
        for html in html_sources:
            match = re.search(pattern, html)
            if match:
                return normalize_url(match.group(0), page.url)

    return None


def write_netscape_cookie_file(cookies: Sequence[Mapping[str, object]], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by TronClass Vimeo Downloader",
    ]

    for cookie in cookies:
        domain_value = cookie.get("domain", "")
        if not isinstance(domain_value, str) or not domain_value:
            continue

        include_subdomains = "TRUE" if domain_value.startswith(".") else "FALSE"
        path_value = cookie.get("path", "/")
        path = path_value if isinstance(path_value, str) and path_value else "/"
        secure = "TRUE" if cookie.get("secure") else "FALSE"

        expires_value = cookie.get("expires", 0)
        if isinstance(expires_value, (int, float)):
            expires = int(expires_value)
        else:
            expires = 0
        if expires < 0:
            expires = 0

        name_value = cookie.get("name", "")
        name = name_value if isinstance(name_value, str) else ""
        stored_value = cookie.get("value", "")
        value = stored_value if isinstance(stored_value, str) else ""

        line = f"{domain_value}\t{include_subdomains}\t{path}\t{secure}\t{expires}\t{name}\t{value}"
        lines.append(line)

    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def flatten_subtitle_tracks(track_map: Mapping[str, object] | None, source: str) -> list[SubtitleRow]:
    rows: list[SubtitleRow] = []
    if not isinstance(track_map, dict):
        return rows

    for lang in sorted(track_map.keys()):
        tracks = track_map.get(lang)
        if not isinstance(tracks, list):
            continue
        track_items = cast(list[object], tracks)
        for raw_track in track_items:
            if not isinstance(raw_track, Mapping):
                continue
            track_data = cast(Mapping[str, object], raw_track)
            rows.append(
                {
                    "來源": source,
                    "語言": str(lang),
                    "格式": str(track_data.get("ext") or ""),
                    "名稱": str(track_data.get("name") or ""),
                    "協定": str(track_data.get("protocol") or ""),
                    "有 URL": "yes" if track_data.get("url") else "no",
                }
            )

    return rows


def collect_subtitle_languages(manual_rows: list[SubtitleRow], auto_rows: list[SubtitleRow]) -> list[str]:
    languages = {
        row.get("語言", "").strip()
        for row in [*manual_rows, *auto_rows]
        if row.get("語言", "").strip()
    }
    return sorted(languages)


def render_copy_languages_widget(languages: list[str]) -> None:
    if not languages:
        return

    joined_codes = ",".join(languages)
    st.text_input("可用字幕語言代碼", value=joined_codes, key="subtitle_language_codes", disabled=True)

    escaped_codes = joined_codes.replace("\\", "\\\\").replace("'", "\\'")
    components.html(
        f"""
        <button id=\"copy-lang-btn\" style=\"padding:8px 12px;border:1px solid #bbb;border-radius:8px;background:#fff;cursor:pointer;\">一鍵複製語言代碼</button>
        <span id=\"copy-lang-status\" style=\"margin-left:8px;color:#2c7a2c;\"></span>
        <script>
            const btn = document.getElementById('copy-lang-btn');
            const status = document.getElementById('copy-lang-status');
            btn.addEventListener('click', async () => {{
                try {{
                    await navigator.clipboard.writeText('{escaped_codes}');
                    status.textContent = '已複製';
                }} catch (err) {{
                    status.textContent = '複製失敗，請手動複製';
                }}
            }});
        </script>
        """,
        height=48,
    )


def inspect_subtitle_tracks(vimeo_url: str, cookie_file: Path, referer_url: str, retry_count: int) -> SubtitleDebugReport:
    opts: dict[str, Any] = {
        "cookiefile": str(cookie_file),
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "retries": retry_count,
        "http_headers": {
            "Referer": referer_url,
        },
    }

    with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
        info = cast(dict[str, Any], ydl.extract_info(vimeo_url, download=False))

    manual_rows = flatten_subtitle_tracks(info.get("subtitles"), source="manual")
    auto_rows = flatten_subtitle_tracks(info.get("automatic_captions"), source="auto")

    return {
        "title": str(info.get("title") or ""),
        "extractor": str(info.get("extractor_key") or ""),
        "manual_rows": manual_rows,
        "auto_rows": auto_rows,
        "inspection_error": "",
    }


def render_subtitle_debug_report(debug_report: SubtitleDebugReport) -> None:
    st.markdown("### 字幕除錯資訊")

    inspection_error = debug_report["inspection_error"]
    if inspection_error:
        st.error(f"無法取得字幕軌清單：{inspection_error}")
        st.warning("這通常代表權限不足、cookie 失效，或影片提供方封鎖了字幕清單。")
        return

    title = debug_report["title"]
    extractor = debug_report["extractor"]
    if title or extractor:
        st.caption(f"yt-dlp 解析來源: {extractor} | 標題: {title}")

    manual_rows = debug_report["manual_rows"]
    auto_rows = debug_report["auto_rows"]

    if manual_rows:
        st.write("手動字幕軌")
        st.dataframe(manual_rows, use_container_width=True)
    else:
        st.write("手動字幕軌：無")

    if auto_rows:
        st.write("自動字幕軌")
        st.dataframe(auto_rows, use_container_width=True)
    else:
        st.write("自動字幕軌：無")

    languages = collect_subtitle_languages(manual_rows, auto_rows)
    if languages:
        render_copy_languages_widget(languages)

    if not manual_rows and not auto_rows:
        st.warning("yt-dlp 未回報任何字幕軌。可能是影片本身無字幕，或當前帳號沒有字幕存取權。")


def resolve_vimeo_and_cookies(
    login_url: str,
    username: str,
    password: str,
    video_page_url: str,
    cookie_file: Path,
    headless: bool,
    status_ui: StatusUI,
) -> str:
    with sync_playwright() as playwright:
        browser = launch_chromium_with_bootstrap(playwright, headless=headless, status_ui=status_ui)
        context = browser.new_context()
        page = context.new_page()

        status_ui.info("1/4 開啟 TronClass 登入頁")
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        selected_user = fill_first(page, USERNAME_SELECTORS, username)
        selected_pass = fill_first(page, PASSWORD_SELECTORS, password)

        if not selected_user or not selected_pass:
            raise RuntimeError("找不到登入欄位，請改用更精準的登入網址，或關閉無頭模式觀察實際欄位。")

        if not click_first(page, SUBMIT_SELECTORS):
            page.keyboard.press("Enter")

        page.wait_for_load_state("networkidle", timeout=30000)

        status_ui.info("2/4 前往含影片的課程頁面")
        page.goto(video_page_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            # Some SPA pages keep long-lived requests; continue with URL probing.
            pass

        vimeo_url = find_vimeo_player_url(page)
        if not vimeo_url:
            raise RuntimeError("找不到 Vimeo 播放器連結。請確認頁面可播放，且影片不是 DRM 保護內容。")

        status_ui.info("3/4 匯出登入 cookies")
        write_netscape_cookie_file(context.cookies(), cookie_file)

        browser.close()

    return vimeo_url


def private_cookie_dir() -> tempfile.TemporaryDirectory[str]:
    # Login cookies stay private to one run: the folder is unique, owner-only and removed when the run ends.
    return tempfile.TemporaryDirectory(prefix="videocap-cookies-", ignore_cleanup_errors=True)


def is_missing_playwright_executable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = [
        "executable doesn't exist",
        "please run the following command to download new browsers",
        "playwright install",
    ]
    return any(marker in message for marker in markers)


def get_missing_playwright_linux_shared_library(exc: Exception) -> str | None:
    message = str(exc)
    if "error while loading shared libraries:" not in message.lower():
        return None

    match = re.search(r"error while loading shared libraries:\s*([^:\s]+)", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return "unknown shared library"


def ensure_playwright_chromium_installed(status_ui: StatusUI) -> None:
    global playwright_chromium_ready
    if playwright_chromium_ready:
        return

    status_ui.info("0/4 偵測到 Playwright Chromium 缺失，正在安裝（首次可能需要 1-3 分鐘）")
    install_cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    result = subprocess.run(
        install_cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    if result.returncode != 0:
        stderr_text = (result.stderr or "").strip()
        stdout_text = (result.stdout or "").strip()
        details = stderr_text or stdout_text or "unknown error"
        raise RuntimeError(f"Playwright Chromium 安裝失敗：{details}")

    playwright_chromium_ready = True
    status_ui.info("0/4 Playwright Chromium 安裝完成，繼續執行登入流程")


def launch_chromium_with_bootstrap(playwright: Playwright, headless: bool, status_ui: StatusUI) -> Browser:
    for attempt in range(2):
        try:
            return playwright.chromium.launch(headless=headless)
        except Exception as exc:
            missing_library = get_missing_playwright_linux_shared_library(exc)
            if missing_library is not None:
                raise RuntimeError(
                    f"Playwright Chromium 啟動失敗：缺少 Linux 系統函式庫 {missing_library}。"
                    "若為 Streamlit Cloud，請在 packages.txt 安裝 libnspr4 與 libnss3 後重新部署。"
                ) from exc
            if attempt == 0 and is_missing_playwright_executable_error(exc):
                ensure_playwright_chromium_installed(status_ui)
                continue
            raise
    raise RuntimeError("無法啟動 Playwright Chromium")


def download_with_ytdlp(
    vimeo_url: str,
    output_dir: Path,
    output_template: str,
    cookie_file: Path,
    referer_url: str,
    download_subtitles: bool,
    subtitle_languages: list[str],
    subtitle_format: str,
    embed_subtitles: bool,
    subtitle_debug_mode: bool,
    retry_count: int,
    progress_ui: ProgressUI,
) -> tuple[Path, bool, bool, SubtitleDebugReport]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_available, _ = get_ffmpeg_status()

    result: dict[str, str] = {}
    subtitle_debug_report: SubtitleDebugReport = {
        "title": "",
        "extractor": "",
        "manual_rows": [],
        "auto_rows": [],
        "inspection_error": "",
    }

    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            speed = data.get("speed")
            if total:
                fraction = min(max(downloaded / total, 0.0), 1.0)
                speed_text = ""
                if speed:
                    speed_text = f" | {speed / (1024 * 1024):.2f} MB/s"
                progress_ui.progress(fraction, text=f"下載中 {fraction * 100:.1f}%{speed_text}")
        elif status == "finished":
            progress_ui.progress(1.0, text="下載完成，正在封裝檔案...")

    ydl_opts: dict[str, Any] = {
        "outtmpl": str(output_dir / output_template),
        "cookiefile": str(cookie_file),
        "format": "bestvideo+bestaudio/best" if ffmpeg_available else "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": retry_count,
        "http_headers": {
            "Referer": referer_url,
        },
        "progress_hooks": [hook],
    }
    if ffmpeg_available:
        ydl_opts["merge_output_format"] = "mp4"

    if download_subtitles:
        ydl_opts["writesubtitles"] = True
        ydl_opts["writeautomaticsub"] = True
        ydl_opts["subtitleslangs"] = subtitle_languages
        ydl_opts["subtitlesformat"] = subtitle_format
        postprocessors: list[dict[str, Any]] = []
        if ffmpeg_available and subtitle_format in {"srt", "vtt"}:
            postprocessors.append({"key": "FFmpegSubtitlesConvertor", "format": subtitle_format, "when": "before_dl"})
        if ffmpeg_available and embed_subtitles:
            # Keep the subtitle files after embedding so they can still be downloaded separately.
            postprocessors.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})
        if postprocessors:
            ydl_opts["postprocessors"] = postprocessors

    if subtitle_debug_mode:
        try:
            subtitle_debug_report = inspect_subtitle_tracks(
                vimeo_url=vimeo_url,
                cookie_file=cookie_file,
                referer_url=referer_url,
                retry_count=retry_count,
            )
        except Exception as exc:
            subtitle_debug_report["inspection_error"] = str(exc)

    with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
        info = cast(dict[str, Any], ydl.extract_info(vimeo_url, download=True))
        filename = str(ydl.prepare_filename(cast(Any, info)))
        has_available_subs = bool(info.get("subtitles") or info.get("automatic_captions"))
        has_requested_subs = bool(info.get("requested_subtitles"))

        requested = info.get("requested_downloads")
        if requested and isinstance(requested, list):
            requested_items = cast(list[object], requested)
            if requested_items and isinstance(requested_items[0], Mapping):
                first_request = cast(Mapping[str, object], requested_items[0])
                filepath = first_request.get("filepath")
                if isinstance(filepath, str) and filepath:
                    filename = filepath

        result["path"] = filename

    return Path(result["path"]).resolve(), has_available_subs, has_requested_subs, subtitle_debug_report


def run_download(
    login_url: str,
    username: str,
    password: str,
    video_page_url: str,
    output_dir: Path,
    output_template: str,
    headless: bool,
    download_subtitles: bool,
    subtitle_languages: list[str],
    subtitle_format: str,
    embed_subtitles: bool,
    subtitle_debug_mode: bool,
    retry_count: int,
    status_ui: StatusUI,
    progress_ui: ProgressUI,
) -> tuple[Path, bool, bool, SubtitleDebugReport]:
    with private_cookie_dir() as cookie_dir:
        cookie_file = Path(cookie_dir) / "cookies.txt"
        vimeo_url = resolve_vimeo_and_cookies(
            login_url=login_url,
            username=username,
            password=password,
            video_page_url=video_page_url,
            cookie_file=cookie_file,
            headless=headless,
            status_ui=status_ui,
        )

        status_ui.info("4/4 使用 yt-dlp 下載 Vimeo 影片")
        return download_with_ytdlp(
            vimeo_url=vimeo_url,
            output_dir=output_dir,
            output_template=output_template,
            cookie_file=cookie_file,
            referer_url=video_page_url,
            download_subtitles=download_subtitles,
            subtitle_languages=subtitle_languages,
            subtitle_format=subtitle_format,
            embed_subtitles=embed_subtitles,
            subtitle_debug_mode=subtitle_debug_mode,
            retry_count=retry_count,
            progress_ui=progress_ui,
        )


def run_subtitle_inspection(
    login_url: str,
    username: str,
    password: str,
    video_page_url: str,
    headless: bool,
    retry_count: int,
    status_ui: StatusUI,
) -> tuple[str, SubtitleDebugReport]:
    with private_cookie_dir() as cookie_dir:
        cookie_file = Path(cookie_dir) / "cookies.txt"
        vimeo_url = resolve_vimeo_and_cookies(
            login_url=login_url,
            username=username,
            password=password,
            video_page_url=video_page_url,
            cookie_file=cookie_file,
            headless=headless,
            status_ui=status_ui,
        )

        status_ui.info("4/4 只檢查字幕軌 (不下載影片)")
        debug_report = inspect_subtitle_tracks(
            vimeo_url=vimeo_url,
            cookie_file=cookie_file,
            referer_url=video_page_url,
            retry_count=retry_count,
        )
    return vimeo_url, debug_report


def parse_subtitle_languages(raw: str) -> list[str]:
    langs = [part.strip() for part in raw.split(",") if part.strip()]
    if not langs:
        return ["all"]
    return langs


def is_streamlit_cloud_environment() -> bool:
    markers = [
        os.getenv("STREAMLIT_RUNTIME_ENV", ""),
        os.getenv("STREAMLIT_SHARING_MODE", ""),
        os.getenv("IS_STREAMLIT_CLOUD", ""),
        os.getenv("STREAMLIT_CLOUD", ""),
        os.getenv("STREAMLIT_ENV", ""),
        os.getenv("ENVIRONMENT", ""),
    ]
    normalized = [value.strip().lower() for value in markers if value]
    if any(value in {"cloud", "true", "1", "sharing", "production", "prod"} for value in normalized):
        return True

    host_markers = [
        os.getenv("HOSTNAME", "").lower(),
        os.getenv("HOST", "").lower(),
        os.getenv("STREAMLIT_SERVER_ADDRESS", "").lower(),
    ]
    if any("streamlit.app" in marker for marker in host_markers if marker):
        return True

    # Streamlit Cloud commonly mounts repositories under /mount/src/<repo>.
    cwd_text = str(Path.cwd()).replace("\\", "/").lower()
    return cwd_text.startswith("/mount/src")


def get_server_output_presets() -> list[str]:
    candidates = [
        (Path.cwd() / "downloads").resolve(),
        Path.cwd().resolve(),
        Path("/tmp/videocap-downloads"),
        Path("/tmp"),
    ]

    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        text = str(path)
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


def detect_runtime_commit_short() -> str:
    env_keys = [
        "SOURCE_COMMIT",
        "GITHUB_SHA",
        "GIT_COMMIT",
        "COMMIT_SHA",
        "STREAMLIT_GIT_SHA",
    ]

    for key in env_keys:
        value = os.getenv(key, "").strip()
        if value:
            return value[:7]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        commit_value = result.stdout.strip()
        if result.returncode == 0 and commit_value:
            return commit_value[:7]
    except Exception:
        pass

    return DEPLOY_VERIFY_MIN_COMMIT


def collect_video_and_subtitle_files(video_file: Path) -> list[Path]:
    if not video_file.exists():
        return []

    files = [video_file]
    base_name = video_file.stem
    directory = video_file.parent

    for candidate in directory.iterdir():
        if not candidate.is_file() or candidate == video_file:
            continue
        if candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue

        candidate_name = candidate.name
        if candidate_name.startswith(f"{base_name}.") or candidate_name.startswith(f"{base_name}_"):
            files.append(candidate)

    files.sort(key=lambda path: (path.suffix.lower(), path.name.lower()))
    return files


def build_video_subtitle_zip(video_file: Path) -> tuple[Optional[bytes], str, list[str]]:
    files = collect_video_and_subtitle_files(video_file)
    if not files:
        return None, "", []

    zip_name = f"{video_file.stem}_bundle.zip"
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, arcname=path.name)

    subtitle_names = [path.name for path in files if path != video_file]
    return buffer.getvalue(), zip_name, subtitle_names


def select_output_directory(initial_dir: str) -> tuple[Optional[str], Optional[str]]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return None, f"無法開啟資料夾選擇器：{exc}"

    try:
        root = tk.Tk()
        root.withdraw()
        cast(Any, root).attributes("-topmost", True)
        selected = filedialog.askdirectory(initialdir=initial_dir or str(Path.cwd()))
        root.destroy()
    except Exception as exc:
        return None, f"開啟資料夾選擇器失敗：{exc}"

    if selected:
        return selected, None
    return None, None


def append_task_log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    logs: list[str] = st.session_state.setdefault("task_logs", [])
    logs.append(f"[{timestamp}] {message}")
    if len(logs) > 100:
        st.session_state["task_logs"] = logs[-100:]


def apply_custom_style() -> None:
    st.markdown(
        """
        <style>
            .stApp {
                background: linear-gradient(165deg, #f4f7fb 0%, #eef2f7 40%, #edf3f5 100%);
                color: #1f2a3d;
            }
            [data-testid="stSidebar"] {
                background: linear-gradient(180deg, #f9fbfd 0%, #f2f6fb 100%);
                border-right: 1px solid #dde6ef;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input,
            [data-testid="stSidebar"] [data-testid="stTextArea"] textarea,
            [data-testid="stSidebar"] [data-testid="stNumberInput"] input {
                background: #ffffff;
                color: #0f2238;
                border: 1px solid #93abc1;
                border-radius: 10px;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input::placeholder,
            [data-testid="stSidebar"] [data-testid="stTextArea"] textarea::placeholder {
                color: #67809b;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input:focus,
            [data-testid="stSidebar"] [data-testid="stTextArea"] textarea:focus,
            [data-testid="stSidebar"] [data-testid="stNumberInput"] input:focus {
                border-color: #0e6a77;
                box-shadow: 0 0 0 2px rgba(14, 106, 119, 0.18);
            }
            [data-testid="stSidebar"] div[data-baseweb="select"] > div,
            [data-testid="stSidebar"] [data-baseweb="base-input"] > div {
                background: #ffffff;
                border: 1px solid #93abc1;
                color: #0f2238;
            }
            [data-testid="stSidebar"] div[data-baseweb="select"] > div:hover,
            [data-testid="stSidebar"] [data-baseweb="base-input"] > div:hover {
                border-color: #6f8ea9;
            }
            [data-testid="stSidebar"] .stButton > button {
                height: 46px;
                border-radius: 10px;
                margin-top: 8px;
                font-weight: 600;
                letter-spacing: 0.02em;
                box-shadow: 0 4px 14px rgba(17, 34, 61, 0.08);
            }
            [data-testid="stSidebar"] .stButton > button:hover {
                transform: translateY(-1px);
            }
            [data-testid="stSidebar"] .stButton + .stButton {
                margin-top: 2px;
            }
            .hero-title {
                font-size: 56px;
                font-weight: 800;
                letter-spacing: -0.02em;
                color: #1d2b3f;
                margin-bottom: 8px;
            }
            .hero-subtitle {
                color: #5f6f85;
                font-size: 19px;
                margin-bottom: 8px;
            }
            .meta-line {
                color: #5f7088;
                font-size: 15px;
                margin-bottom: 18px;
            }
            .meta-panel {
                margin: 12px 0 22px;
                padding: 18px 20px;
                background: linear-gradient(135deg, rgba(255, 255, 255, 0.98), rgba(232, 246, 248, 0.9));
                border: 1px solid #d7e7ee;
                border-radius: 18px;
                box-shadow: 0 16px 34px rgba(18, 37, 66, 0.08);
            }
            .meta-panel-caption {
                margin-bottom: 12px;
                color: #5d738d;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.14em;
                text-transform: uppercase;
            }
            .badge-row {
                display: flex;
                flex-wrap: wrap;
                gap: 12px;
            }
            .badge-row-primary {
                margin-bottom: 14px;
            }
            .meta-badge {
                display: flex;
                align-items: center;
                gap: 10px;
                flex: 1 1 210px;
                min-width: 0;
                padding: 12px 14px;
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid #d5e5ec;
                border-radius: 14px;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.65);
            }
            .badge-dot {
                width: 10px;
                height: 10px;
                flex: 0 0 10px;
                border-radius: 999px;
                background: linear-gradient(180deg, #11a0b1 0%, #0d6e7a 100%);
                box-shadow: 0 0 0 4px rgba(17, 160, 177, 0.12);
            }
            .meta-badge-label {
                color: #5d7086;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.04em;
                white-space: nowrap;
            }
            .meta-badge-value {
                min-width: 0;
                color: #17314d;
                font-size: 15px;
                font-weight: 700;
            }
            .meta-grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
                gap: 12px;
            }
            .meta-card {
                min-width: 0;
                padding: 14px 16px;
                background: rgba(245, 250, 252, 0.92);
                border: 1px solid #d5e4eb;
                border-radius: 14px;
            }
            .meta-card-label {
                margin-bottom: 6px;
                color: #61758f;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.04em;
                text-transform: uppercase;
            }
            .meta-card-value {
                color: #163049;
                font-size: 14px;
                font-weight: 700;
                line-height: 1.45;
                word-break: break-word;
            }
            .meta-card-value.mono {
                font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
                font-size: 13px;
            }
            @media (max-width: 900px) {
                .hero-title {
                    font-size: 42px;
                }
                .hero-subtitle {
                    font-size: 17px;
                }
                .meta-panel {
                    padding: 16px;
                }
                .meta-badge {
                    flex-basis: 100%;
                }
                .meta-grid {
                    grid-template-columns: 1fr;
                }
            }
            .step-box {
                border-left: 4px solid #0a7b88;
                background: #f8fbfd;
                border-radius: 10px;
                padding: 14px 16px;
                min-height: 64px;
                color: #35465d;
                box-shadow: 0 8px 24px rgba(11, 36, 71, 0.05);
            }
            .step-box.active {
                background: #e8f6f8;
                border-left-color: #0b5f6c;
                color: #0d4f59;
            }
            .status-card {
                background: #ffffff;
                border: 1px solid #deebf3;
                border-radius: 14px;
                padding: 14px 16px;
                box-shadow: 0 10px 24px rgba(18, 34, 58, 0.06);
                margin-bottom: 10px;
            }
            .log-box {
                max-height: 280px;
                overflow-y: auto;
                background: #f8fbfd;
                border: 1px solid #d9e7f0;
                border-radius: 10px;
                padding: 12px;
                font-family: Consolas, monospace;
                font-size: 13px;
                color: #27415a;
                line-height: 1.6;
            }
            .health-card {
                background: #ffffff;
                border: 1px solid #dbe9f2;
                border-radius: 12px;
                padding: 12px 14px;
                margin-bottom: 10px;
                box-shadow: 0 8px 20px rgba(21, 43, 74, 0.06);
            }
            .health-title {
                font-size: 14px;
                color: #3b5271;
                margin-bottom: 8px;
                font-weight: 700;
            }
            .health-lights {
                display: flex;
                gap: 16px;
                align-items: center;
                margin-bottom: 8px;
            }
            .light-item {
                display: flex;
                align-items: center;
                gap: 6px;
                font-size: 12px;
                color: #4c607a;
            }
            .light-dot {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: #cbd8e4;
                box-shadow: inset 0 0 0 1px #c3d3df;
            }
            .light-dot.on.green {
                background: #22c55e;
                box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.15);
            }
            .light-dot.on.yellow {
                background: #f59e0b;
                box-shadow: 0 0 0 5px rgba(245, 158, 11, 0.18);
            }
            .light-dot.on.red {
                background: #ef4444;
                box-shadow: 0 0 0 5px rgba(239, 68, 68, 0.16);
            }
            .health-message {
                color: #2f4866;
                font-size: 13px;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_workflow_steps(active_step: int) -> None:
    labels = ["解析連結", "下載影片", "處理字幕", "完成交付"]
    cols = st.columns(4)
    for idx, label in enumerate(labels, start=1):
        css_class = "step-box active" if idx == active_step else "step-box"
        cols[idx - 1].markdown(
            f"<div class='{css_class}'><strong>{idx}. {label}</strong></div>",
            unsafe_allow_html=True,
        )


def render_meta_badges(
    version: str,
    updated_at: str,
    deploy_target: str,
    runtime_commit: str,
    min_commit: str,
    deploy_marker: str,
) -> None:
    st.markdown(
        (
            "<div class='meta-panel'>"
            "<div class='meta-panel-caption'>Deployment Overview</div>"
            "<div class='badge-row badge-row-primary'>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>版本</span>"
            f"<span class='meta-badge-value'>{version}</span>"
            "</div>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>最後更新</span>"
            f"<span class='meta-badge-value'>{updated_at}</span>"
            "</div>"
            "<div class='meta-badge'>"
            "<span class='badge-dot'></span>"
            "<span class='meta-badge-label'>部署方式</span>"
            f"<span class='meta-badge-value'>{deploy_target}</span>"
            "</div>"
            "</div>"
            "<div class='meta-grid'>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>Runtime Commit</div>"
            f"<div class='meta-card-value mono'>{runtime_commit}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>Baseline Commit</div>"
            f"<div class='meta-card-value mono'>{min_commit}</div>"
            "</div>"
            "<div class='meta-card'>"
            "<div class='meta-card-label'>Deploy Marker</div>"
            f"<div class='meta-card-value mono'>{deploy_marker}</div>"
            "</div>"
            "</div>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def health_panel_html(status: str, message: str) -> str:
    green_class = "light-dot on green" if status == "green" else "light-dot"
    yellow_class = "light-dot on yellow" if status == "yellow" else "light-dot"
    red_class = "light-dot on red" if status == "red" else "light-dot"
    safe_message = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if message else "尚未執行健康檢查"
    )
    return (
        "<div class='health-card'>"
        "<div class='health-title'>連結健康狀態</div>"
        "<div class='health-lights'>"
        f"<div class='light-item'><span class='{green_class}'></span><span>正常</span></div>"
        f"<div class='light-item'><span class='{yellow_class}'></span><span>警告</span></div>"
        f"<div class='light-item'><span class='{red_class}'></span><span>失敗</span></div>"
        "</div>"
        f"<div class='health-message'>{safe_message}</div>"
        "</div>"
    )


def render_final_download_actions(output_file: Path, is_cloud: bool) -> None:
    if not output_file.exists():
        st.warning("找不到影片檔，請重試下載。")
        return

    zip_data, zip_name, subtitle_names = build_video_subtitle_zip(output_file)
    bundled_files = collect_video_and_subtitle_files(output_file)
    subtitle_files = [path for path in bundled_files if path != output_file]
    key_base = re.sub(r"[^A-Za-z0-9_]+", "_", output_file.stem).strip("_") or "video"

    st.markdown("完成後下載選項")

    if zip_data:
        st.download_button(
            label="下載影片+字幕.zip",
            data=zip_data,
            file_name=zip_name,
            mime="application/zip",
            use_container_width=True,
            key=f"download_zip_{key_base}",
        )
        if subtitle_names:
            st.caption(f"壓縮檔已包含影片與 {len(subtitle_names)} 個字幕檔。")
        else:
            st.caption("未偵測到字幕檔，壓縮檔目前僅包含影片。")
    else:
        st.button(
            "下載影片+字幕.zip",
            use_container_width=True,
            disabled=True,
            key=f"download_zip_disabled_{key_base}",
        )
        st.caption("目前無法建立影片+字幕壓縮檔。")

    show_separate_key = f"show_separate_downloads_{key_base}"
    open_separate = st.button(
        "下載影片+字幕(影片及字幕檔案分別下載)",
        use_container_width=True,
        key=f"show_separate_btn_{key_base}",
    )
    if open_separate:
        st.session_state[show_separate_key] = not st.session_state.get(show_separate_key, False)

    if st.session_state.get(show_separate_key, False):
        st.download_button(
            label=f"下載影片 ({output_file.name})",
            data=output_file.read_bytes(),
            file_name=output_file.name,
            mime="video/mp4",
            use_container_width=True,
            key=f"download_video_separate_{key_base}",
        )
        if subtitle_files:
            for idx, subtitle_path in enumerate(subtitle_files, start=1):
                st.download_button(
                    label=f"下載字幕 {idx}: {subtitle_path.name}",
                    data=subtitle_path.read_bytes(),
                    file_name=subtitle_path.name,
                    mime="text/plain",
                    use_container_width=True,
                    key=f"download_subtitle_{key_base}_{idx}",
                )
        else:
            st.info("未偵測到可下載字幕檔。")

    st.download_button(
        label="下載影片",
        data=output_file.read_bytes(),
        file_name=output_file.name,
        mime="video/mp4",
        use_container_width=True,
        key=f"download_video_only_{key_base}",
    )
    if is_cloud:
        st.caption("部署環境無法直接寫入你的電腦資料夾，請使用上方按鈕下載。")


def main() -> None:
    st.set_page_config(
        page_title="TronClass 影片下載平台",
        page_icon="🎬",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    apply_custom_style()

    st.session_state.setdefault("output_dir_raw", str(Path.cwd() / "downloads"))
    st.session_state.setdefault("task_logs", [])
    st.session_state.setdefault("active_step", 1)
    st.session_state.setdefault("stage_progress", 0.0)
    st.session_state.setdefault("download_progress", 0.0)
    st.session_state.setdefault("last_status", "等待任務開始")
    st.session_state.setdefault("health_status", "")
    st.session_state.setdefault("health_message", "尚未執行健康檢查")
    st.session_state.setdefault("last_output_file", "")
    is_cloud = is_streamlit_cloud_environment()
    ffmpeg_available, ffmpeg_status = get_ffmpeg_status()

    with st.sidebar:
        st.markdown("## 任務設定")
        work_mode = st.radio("作業模式", options=["單一連結", "批次連結"], index=0)
        if work_mode == "批次連結":
            st.caption("批次模式即將推出，現階段先使用單一連結流程。")

        login_url = st.text_input(
            "TronClass 登入網址",
            value="https://dstar.tronclass.com/login?next=/user/index#/",
            placeholder="https://dstar.tronclass.com/login?next=/user/index#/",
        )
        username = st.text_input("TronClass 帳號")
        password = st.text_input("TronClass 密碼", type="password")
        video_page_url = st.text_input("課程影片頁網址", placeholder="https://...")

        naming_rule = st.radio("輸出檔名規則", options=["沿用原始檔名", "原始檔名加日期", "自訂名稱"], index=0)
        custom_name = ""
        if naming_rule == "自訂名稱":
            custom_name = st.text_input("自訂檔名", value="lesson_video")

        if is_cloud:
            server_presets = get_server_output_presets()
            _cur = st.session_state.get("output_dir_raw", server_presets[0] if server_presets else "")
            _def_idx = server_presets.index(_cur) if _cur in server_presets else 0
            selected_server_dir = st.selectbox(
                "伺服器暫存目錄",
                options=server_presets,
                index=_def_idx,
                key="server_output_dir_select",
            )

            st.text_input("下載資料夾", value=selected_server_dir, disabled=True, key="cloud_output_dir_display")
            st.caption("雲端模式：下載完成後請使用下方下載按鈕存到本機。")

            output_dir_raw = selected_server_dir
        else:
            output_dir_raw = st.text_input("下載資料夾", key="output_dir_raw")

        st.selectbox("輸出格式", options=["MP4"], index=0, disabled=True)
        retry_count = st.slider("重試次數", min_value=1, max_value=10, value=3)
        save_in_server_dir = st.checkbox("另存至伺服器工作目錄", value=True)

        st.markdown("### 字幕設定")
        headless = st.checkbox("無頭模式", value=True)
        download_subtitles = st.checkbox("下載字幕", value=True)
        subtitle_languages_raw = st.text_input("字幕語言代碼", value="all")
        subtitle_format = st.selectbox("字幕格式", options=["srt", "vtt", "best"], index=0)
        embed_subtitles = st.checkbox("嵌入字幕到 mp4", value=True)
        if not is_cloud and sys.platform == "darwin":
            if ffmpeg_available:
                st.info("macOS 已偵測到 ffmpeg，會使用最佳畫質流程；若勾選字幕嵌入，會直接封裝進 mp4。")
            else:
                st.info("macOS 目前會走相容模式：仍可下載影片與外掛字幕檔，但不會做高畫質合併或字幕嵌入。")
        if not is_cloud and not ffmpeg_available:
            if sys.platform == "darwin":
                st.warning(
                    "未偵測到 ffmpeg。macOS 本機仍可下載，但會改用相容模式，只抓單一影片檔並略過字幕嵌入。若要最佳畫質或嵌入字幕，請先執行 brew install ffmpeg。下載完成後仍可用下方按鈕把影片與字幕存到本機。"
                )
            else:
                st.warning(
                    "未偵測到 ffmpeg。本機仍可下載，但會改用相容模式，只抓單一影片檔並略過字幕嵌入。若要最佳畫質或嵌入字幕，請先安裝 ffmpeg。"
                )
            st.caption(f"ffmpeg 狀態：{ffmpeg_status}")
        subtitle_debug_mode = st.checkbox("除錯模式：顯示字幕軌", value=False)

        start_download = st.button("開始下載", type="primary", use_container_width=True)
        inspect_only = st.button("連結健康檢查", use_container_width=True)
        clear_logs = st.button("清除任務歷程", use_container_width=True)

        st.markdown("---")
        st.caption("部署維運")
        st.caption("推送到 main 分支可自動部署。")

    if clear_logs:
        st.session_state["task_logs"] = []
        st.session_state["active_step"] = 1
        st.session_state["stage_progress"] = 0.0
        st.session_state["download_progress"] = 0.0
        st.session_state["last_status"] = "等待任務開始"
        st.session_state["health_status"] = ""
        st.session_state["health_message"] = "尚未執行健康檢查"
        st.session_state["last_output_file"] = ""
        for key in list(st.session_state.keys()):
            if isinstance(key, str) and key.startswith("show_separate_downloads_"):
                st.session_state.pop(key, None)
        st.success("任務歷程已清除。")

    st.markdown("<div class='hero-title'>TronClass 影片下載平台</div>", unsafe_allow_html=True)
    st.markdown("<div class='hero-subtitle'>課程頁面內嵌 Vimeo 影片與字幕一站式下載</div>", unsafe_allow_html=True)
    runtime_commit = detect_runtime_commit_short()
    render_meta_badges(
        version=APP_VERSION,
        updated_at=APP_UPDATED_AT,
        deploy_target="main 分支自動部署",
        runtime_commit=runtime_commit,
        min_commit=DEPLOY_VERIFY_MIN_COMMIT,
        deploy_marker=DEPLOY_MARKER,
    )

    status_placeholder = st.empty()
    health_placeholder = st.empty()
    status_placeholder.markdown(
        f"<div class='status-card'><strong>{st.session_state.get('last_status', '等待任務開始')}</strong></div>",
        unsafe_allow_html=True,
    )
    health_placeholder.markdown(
        health_panel_html(
            st.session_state.get("health_status", ""),
            st.session_state.get("health_message", "尚未執行健康檢查"),
        ),
        unsafe_allow_html=True,
    )
    stage_progress_ui = st.progress(
        st.session_state.get("stage_progress", 0.0),
        text=f"流程進度: {int(st.session_state.get('stage_progress', 0.0) * 100)}%",
    )
    download_progress_ui = st.progress(
        st.session_state.get("download_progress", 0.0),
        text=f"下載進度: {int(st.session_state.get('download_progress', 0.0) * 100)}%",
    )

    with st.expander("作業說明", expanded=False):
        logs = st.session_state.get("task_logs", [])
        if logs:
            escaped_logs = "<br>".join(
                line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                for line in logs[-80:]
            )
            st.markdown(f"<div class='log-box'>{escaped_logs}</div>", unsafe_allow_html=True)
        else:
            st.caption("尚無任務紀錄。")

    class StatusBridge:
        def __init__(self, placeholder: Any, stage_ui: Any) -> None:
            self.placeholder = placeholder
            self.stage_ui = stage_ui

        def info(self, message: str) -> None:
            st.session_state["last_status"] = message
            append_task_log(message)
            step_match = re.match(r"(\d)/(\d)\s+(.+)", message)
            if step_match:
                current = int(step_match.group(1))
                total = max(int(step_match.group(2)), 1)
                stage_fraction = min(max(current / total, 0.0), 1.0)
                st.session_state["active_step"] = min(max(current, 1), 4)
                st.session_state["stage_progress"] = stage_fraction
                self.stage_ui.progress(stage_fraction, text=f"流程進度: {int(stage_fraction * 100)}%")
            self.placeholder.markdown(
                f"<div class='status-card'><strong>{message}</strong></div>",
                unsafe_allow_html=True,
            )

    class DownloadProgressBridge:
        def __init__(self, progress_ui: Any) -> None:
            self.progress_ui = progress_ui

        def progress(self, value: float, text: str = "") -> None:
            clamped = min(max(value, 0.0), 1.0)
            st.session_state["download_progress"] = clamped
            self.progress_ui.progress(clamped, text=text or f"下載進度: {int(clamped * 100)}%")

    if naming_rule == "沿用原始檔名":
        output_template = "%(title)s.%(ext)s"
    elif naming_rule == "原始檔名加日期":
        output_template = "%(title)s_%(upload_date)s.%(ext)s"
    else:
        safe_name = re.sub(r"[^A-Za-z0-9_\-.]+", "_", custom_name.strip())
        if not safe_name:
            safe_name = "lesson_video"
        output_template = f"{safe_name}_%(id)s.%(ext)s"

    output_dir_candidate = output_dir_raw.strip() if output_dir_raw.strip() else str(Path.cwd() / "downloads")
    output_dir = Path(output_dir_candidate).expanduser().resolve()
    if save_in_server_dir and not output_dir_candidate:
        output_dir = (Path.cwd() / "downloads").resolve()

    if start_download or inspect_only:
        if not all([login_url.strip(), username.strip(), password.strip(), video_page_url.strip()]):
            st.error("請填寫所有必填欄位。")
            return

        st.session_state["active_step"] = 1
        st.session_state["stage_progress"] = 0.0
        st.session_state["download_progress"] = 0.0
        st.session_state["last_status"] = "任務啟動中..."
        append_task_log("任務啟動")

        status_ui = StatusBridge(status_placeholder, stage_progress_ui)
        progress_ui = DownloadProgressBridge(download_progress_ui)

        try:
            if inspect_only:
                _, subtitle_debug_report = run_subtitle_inspection(
                    login_url=login_url.strip(),
                    username=username.strip(),
                    password=password,
                    video_page_url=video_page_url.strip(),
                    headless=headless,
                    retry_count=retry_count,
                    status_ui=status_ui,
                )
                manual_rows = subtitle_debug_report.get("manual_rows") or []
                auto_rows = subtitle_debug_report.get("auto_rows") or []
                inspection_error = str(subtitle_debug_report.get("inspection_error") or "")
                if inspection_error:
                    st.session_state["health_status"] = "red"
                    st.session_state["health_message"] = f"健康檢查失敗：{inspection_error}"
                elif manual_rows or auto_rows:
                    st.session_state["health_status"] = "green"
                    st.session_state["health_message"] = "連結可用，且可讀取字幕軌。"
                else:
                    st.session_state["health_status"] = "yellow"
                    st.session_state["health_message"] = "連結可用，但未偵測到字幕軌或權限受限。"

                health_placeholder.markdown(
                    health_panel_html(st.session_state["health_status"], st.session_state["health_message"]),
                    unsafe_allow_html=True,
                )
                st.session_state["stage_progress"] = 1.0
                st.session_state["active_step"] = 4
                stage_progress_ui.progress(1.0, text="流程進度: 100%")
                st.success("連結健康檢查完成，未下載影片。")
                render_subtitle_debug_report(subtitle_debug_report)
            else:
                st.session_state["health_status"] = ""
                st.session_state["health_message"] = "尚未執行健康檢查"
                health_placeholder.markdown(
                    health_panel_html(st.session_state["health_status"], st.session_state["health_message"]),
                    unsafe_allow_html=True,
                )
                output_file, has_available_subs, has_requested_subs, subtitle_debug_report = run_download(
                    login_url=login_url.strip(),
                    username=username.strip(),
                    password=password,
                    video_page_url=video_page_url.strip(),
                    output_dir=output_dir,
                    output_template=output_template,
                    headless=headless,
                    download_subtitles=download_subtitles,
                    subtitle_languages=parse_subtitle_languages(subtitle_languages_raw),
                    subtitle_format=subtitle_format,
                    embed_subtitles=embed_subtitles,
                    subtitle_debug_mode=subtitle_debug_mode,
                    retry_count=retry_count,
                    status_ui=status_ui,
                    progress_ui=progress_ui,
                )

                progress_ui.progress(1.0, text="下載進度: 100%")
                st.session_state["stage_progress"] = 1.0
                st.session_state["active_step"] = 4
                stage_progress_ui.progress(1.0, text="流程進度: 100%")

                st.success(f"下載完成：{output_file}")
                append_task_log(f"下載完成: {output_file}")
                st.session_state["last_output_file"] = str(output_file)
                render_final_download_actions(output_file, is_cloud)

                if download_subtitles:
                    if has_requested_subs:
                        st.info("字幕已下載。若未嵌入，字幕檔會與影片放在同資料夾。")
                    elif has_available_subs:
                        st.warning("偵測到字幕軌，但未成功取得字幕檔，請改用 best 或 vtt 再試。")
                    else:
                        st.warning("此影片來源未提供可下載字幕 (或權限不足)。")

                if subtitle_debug_mode:
                    render_subtitle_debug_report(subtitle_debug_report)
        except PlaywrightTimeoutError:
            error_message = "網頁操作逾時，請確認網址正確與網路連線。"
            append_task_log(error_message)
            if inspect_only:
                st.session_state["health_status"] = "red"
                st.session_state["health_message"] = error_message
                health_placeholder.markdown(
                    health_panel_html(st.session_state["health_status"], st.session_state["health_message"]),
                    unsafe_allow_html=True,
                )
            st.error(error_message)
        except Exception as exc:
            error_message = f"下載失敗：{exc}"
            append_task_log(error_message)
            if inspect_only:
                st.session_state["health_status"] = "red"
                st.session_state["health_message"] = str(exc)
                health_placeholder.markdown(
                    health_panel_html(st.session_state["health_status"], st.session_state["health_message"]),
                    unsafe_allow_html=True,
                )
            st.error(error_message)

    if not (start_download or inspect_only):
        saved_output_file = str(st.session_state.get("last_output_file", "")).strip()
        if saved_output_file:
            saved_path = Path(saved_output_file)
            if saved_path.exists():
                st.success(f"下載完成：{saved_path}")
                render_final_download_actions(saved_path, is_cloud)
            else:
                st.session_state["last_output_file"] = ""


if __name__ == "__main__":
    main()
