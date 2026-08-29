import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import ctypes
from ctypes import wintypes

from PySide6.QtCore import QRectF, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMessageBox, QVBoxLayout, QWidget
try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:
    QSvgRenderer = None
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from werkzeug.serving import make_server

from app import SETTINGS, app, init_engine, set_update_callbacks, setup_engine_callbacks
from runtime_paths import RESOURCE_DIR
from version import (
    APP_NAME,
    APP_UPDATE_ASSET_NAME,
    APP_UPDATE_RELEASES_URL,
    APP_UPDATE_REPO,
    APP_VERSION,
    app_display_name,
)


HOST = "127.0.0.1"
# Preferred port, not a requirement: 5000 is crowded on Windows (Logitech
# G HUB's CS:GO Arx applet takes it, and so do a few dev servers), and binding
# over an exclusive socket there fails with "access forbidden" instead of
# "already in use". Fall back to the next free port rather than refusing to
# start. DDMX_PORT forces one for good.
PREFERRED_PORT = 5000
PORT = PREFERRED_PORT
URL = f"http://{HOST}:{PORT}/"
META_URL = f"{URL}api/meta"


def _port_is_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: we want to know whether make_server could really
        # take it, and on Windows reuse would happily bind over a live socket.
        probe.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def choose_port() -> int:
    """Settle on a port and point the app's URLs at it."""
    global PORT, URL, META_URL
    forced = os.environ.get("DDMX_PORT", "").strip()
    if forced.isdigit():
        candidates = [int(forced)]
    else:
        candidates = [PREFERRED_PORT] + [PREFERRED_PORT + i for i in range(1, 21)]
    chosen = next((p for p in candidates if _port_is_free(p)), None)
    if chosen is None:
        chosen = candidates[0]      # let make_server report the real error
    PORT = chosen
    URL = f"http://{HOST}:{PORT}/"
    META_URL = f"{URL}api/meta"
    return PORT
_popup_views = []
MAX_LOAD_ATTEMPTS = 40
LOAD_RETRY_MS = 700
STARTUP_FORCE_RELOAD_MS = 1200
BASE_DIR = RESOURCE_DIR
ICON_SVG_PATH = os.path.join(BASE_DIR, "static", "favicon.svg")
ICON_ICO_PATH = os.path.join(BASE_DIR, "static", "favicon.ico")
APP_ICON = None
SERVER_START_TIMEOUT = 20.0
GUI_POST_READY_DELAY_SEC = 1.0
GUI_SPLASH_MIN_VISIBLE_MS = 2000
SERVER_READY = threading.Event()
SERVER_FAILED = threading.Event()
SERVER_ERROR: Exception | None = None
HTTP_SERVER = None
REQUESTED_APP_QUIT = threading.Event()
UPDATE_STATE_LOCK = threading.RLock()
UPDATE_STATE = {
    "supported": True,
    "install_supported": bool(getattr(sys, "frozen", False)),
    "checking": False,
    "available": False,
    "installing": False,
    "current_version": APP_VERSION,
    "latest_version": APP_VERSION,
    "release_name": "",
    "release_notes": "",
    "release_url": APP_UPDATE_RELEASES_URL,
    "download_url": "",
    "asset_name": APP_UPDATE_ASSET_NAME,
    "repo": APP_UPDATE_REPO,
    "error": "",
    "last_checked_at": 0,
}


def _version_key(raw: str) -> tuple[int, ...]:
    text = str(raw or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    parts = [int(part) for part in re.findall(r"\d+", text)]
    return tuple(parts or [0])


def _is_remote_version_newer(remote: str, current: str) -> bool:
    left = list(_version_key(remote))
    right = list(_version_key(current))
    width = max(len(left), len(right))
    left.extend([0] * (width - len(left)))
    right.extend([0] * (width - len(right)))
    return tuple(left) > tuple(right)


def _clean_version(raw: str) -> str:
    text = str(raw or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    return text or APP_VERSION


def _update_state_snapshot() -> dict:
    with UPDATE_STATE_LOCK:
        return dict(UPDATE_STATE)


def _set_update_state(**changes) -> dict:
    with UPDATE_STATE_LOCK:
        UPDATE_STATE.update(changes)
        return dict(UPDATE_STATE)


def _fetch_latest_release_data() -> dict:
    url = f"https://api.github.com/repos/{APP_UPDATE_REPO}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    assets = payload.get("assets") or []
    chosen = None
    for asset in assets:
        if str(asset.get("name") or "").strip().lower() == APP_UPDATE_ASSET_NAME.lower():
            chosen = asset
            break
    if chosen is None:
        for asset in assets:
            name = str(asset.get("name") or "").strip().lower()
            if name.endswith(".exe"):
                chosen = asset
                break

    latest_version = _clean_version(payload.get("tag_name") or payload.get("name") or APP_VERSION)
    return {
        "latest_version": latest_version,
        "release_name": str(payload.get("name") or latest_version),
        "release_notes": str(payload.get("body") or "").strip(),
        "release_url": str(payload.get("html_url") or APP_UPDATE_RELEASES_URL),
        "download_url": str((chosen or {}).get("browser_download_url") or "").strip(),
        "asset_name": str((chosen or {}).get("name") or APP_UPDATE_ASSET_NAME).strip() or APP_UPDATE_ASSET_NAME,
    }


def get_update_status() -> dict:
    return _update_state_snapshot()


def check_for_updates(manual: bool = True) -> dict:
    _set_update_state(checking=True, error="")
    try:
        release = _fetch_latest_release_data()
        available = _is_remote_version_newer(release["latest_version"], APP_VERSION)
        return _set_update_state(
            checking=False,
            available=available,
            installing=False,
            current_version=APP_VERSION,
            latest_version=release["latest_version"],
            release_name=release["release_name"],
            release_notes=release["release_notes"],
            release_url=release["release_url"],
            download_url=release["download_url"],
            asset_name=release["asset_name"],
            repo=APP_UPDATE_REPO,
            error="",
            last_checked_at=int(time.time() * 1000),
            install_supported=bool(getattr(sys, "frozen", False)),
            supported=True,
        )
    except Exception as exc:
        return _set_update_state(
            checking=False,
            installing=False,
            error=str(exc),
            last_checked_at=int(time.time() * 1000),
            install_supported=bool(getattr(sys, "frozen", False)),
            supported=True,
        )


def install_update() -> dict:
    if not getattr(sys, "frozen", False):
        return _set_update_state(
            install_supported=False,
            installing=False,
            error="Install update is only available from the packaged .exe build.",
        )

    state = _update_state_snapshot()
    if not state.get("available"):
        state = check_for_updates(manual=True)
    if state.get("error"):
        return state
    if not state.get("available"):
        return _set_update_state(installing=False, error="No update is currently available.")

    download_url = str(state.get("download_url") or "").strip()
    if not download_url:
        return _set_update_state(installing=False, error="No downloadable .exe asset was found in the latest release.")

    target_exe = os.path.abspath(sys.executable)
    parent_dir = os.path.dirname(target_exe)
    temp_dir = tempfile.mkdtemp(prefix="ddmx_update_")
    asset_name = str(state.get("asset_name") or APP_UPDATE_ASSET_NAME).strip() or APP_UPDATE_ASSET_NAME
    downloaded_exe = os.path.join(temp_dir, asset_name)
    updater_cmd = os.path.join(temp_dir, "apply_update.cmd")
    backup_exe = f"{target_exe}.bak"
    pid = os.getpid()

    try:
        req = urllib.request.Request(
            download_url,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp, open(downloaded_exe, "wb") as out:
            out.write(resp.read())

        cmd_lines = [
            "@echo off",
            "setlocal enableextensions",
            f"set \"TARGET={target_exe}\"",
            f"set \"SOURCE={downloaded_exe}\"",
            f"set \"BACKUP={backup_exe}\"",
            f"set \"PID={pid}\"",
            "for /l %%I in (1,1,90) do (",
            "  tasklist /FI \"PID eq %PID%\" | find \" %PID% \" >nul",
            "  if errorlevel 1 goto ready",
            "  timeout /t 1 /nobreak >nul",
            ")",
            ":ready",
            "copy /Y \"%TARGET%\" \"%BACKUP%\" >nul 2>nul",
            "copy /Y \"%SOURCE%\" \"%TARGET%\" >nul",
            "if errorlevel 1 goto fail",
            "start \"\" \"%TARGET%\"",
            "exit /b 0",
            ":fail",
            "copy /Y \"%BACKUP%\" \"%TARGET%\" >nul 2>nul",
            "exit /b 1",
        ]
        with open(updater_cmd, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\r\n".join(cmd_lines) + "\r\n")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        subprocess.Popen(
            ["cmd", "/c", updater_cmd],
            cwd=parent_dir,
            close_fds=True,
            creationflags=creationflags,
        )
    except Exception as exc:
        return _set_update_state(installing=False, error=str(exc))

    REQUESTED_APP_QUIT.set()
    return _set_update_state(
        installing=True,
        error="",
        install_supported=True,
    )


def start_server() -> None:
    global HTTP_SERVER, SERVER_ERROR
    try:
        init_engine()
        setup_engine_callbacks()
        set_update_callbacks(
            status_fn=get_update_status,
            check_fn=check_for_updates,
            install_fn=install_update,
        )
        # PORT is settled by choose_port() before this thread starts; call it
        # again only if something ran the server on its own.
        HTTP_SERVER = make_server(HOST, PORT if PORT else choose_port(), app, threaded=True)
        SERVER_READY.set()
        HTTP_SERVER.serve_forever()
    except Exception as exc:
        SERVER_ERROR = exc
        SERVER_FAILED.set()
        SERVER_READY.set()


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_for_http(url: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if SERVER_FAILED.is_set():
            return False
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    data = resp.read(4096)
                    body = data.lower()
                    if b"<html" in body or b"<!doctype" in body or body.strip().startswith(b"{"):
                        return True
        except Exception:
            time.sleep(0.2)
    return False


def wait_for_server_ready(timeout: float = SERVER_START_TIMEOUT) -> tuple[bool, str]:
    ready = SERVER_READY.wait(timeout)
    if not ready:
        return False, f"Server start timed out after {timeout:.0f}s"
    if SERVER_FAILED.is_set():
        return False, f"Server failed to start: {SERVER_ERROR!r}"
    if not wait_for_port(HOST, PORT, timeout=timeout):
        return False, "Server port did not open in time"
    if not wait_for_http(META_URL, timeout=timeout):
        if SERVER_FAILED.is_set():
            return False, f"Server failed to start: {SERVER_ERROR!r}"
        return False, "Server HTTP health check did not become ready"
    return True, ""


def stop_server() -> None:
    global HTTP_SERVER
    try:
        if HTTP_SERVER is not None:
            HTTP_SERVER.shutdown()
            HTTP_SERVER.server_close()
    except Exception:
        pass
    HTTP_SERVER = None


def build_svg_icon(svg_path: str) -> QIcon | None:
    if not svg_path or not os.path.exists(svg_path):
        return None
    if QSvgRenderer is None:
        return None
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        return None
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def build_svg_pixmap(svg_path: str, size: int = 96) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    if QSvgRenderer is None or not svg_path or not os.path.exists(svg_path):
        return pixmap
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        return pixmap
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def ensure_ico(svg_path: str, ico_path: str) -> bool:
    if not svg_path or not os.path.exists(svg_path):
        return False
    if os.path.exists(ico_path):
        return True
    if QSvgRenderer is None:
        return False
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        return False
    size = 256
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap.save(ico_path, "ICO")


def set_windows_app_id(app_id: str) -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def set_taskbar_icon(view: QWebEngineView, ico_path: str) -> None:
    if sys.platform != "win32":
        return
    if not ico_path or not os.path.exists(ico_path):
        return
    try:
        hwnd = int(view.winId())
    except Exception:
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    hicon = user32.LoadImageW(
        None,
        ico_path,
        1,
        0,
        0,
        0x00000010
    )
    if not hicon:
        return
    user32.SendMessageW(hwnd, 0x0080, 1, hicon)
    user32.SendMessageW(hwnd, 0x0080, 0, hicon)


class StartupSplash(QWidget):
    def __init__(self, logo_pixmap: QPixmap | None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.SplashScreen)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: #000000;")
        self.setFixedSize(560, 180)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(22)

        logo = QLabel(self)
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(96, 96)
        if logo_pixmap is not None and not logo_pixmap.isNull():
            logo.setPixmap(logo_pixmap)

        text_wrap = QWidget(self)
        text_layout = QVBoxLayout(text_wrap)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.setAlignment(Qt.AlignVCenter)

        title = QLabel("DDMX", text_wrap)
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title.setStyleSheet("color: #ffffff;")
        title_font = QFont("Segoe UI", 28)
        title_font.setWeight(QFont.DemiBold)
        title.setFont(title_font)

        version = QLabel(APP_VERSION, text_wrap)
        version.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        version.setStyleSheet("color: #94a3b8;")
        version_font = QFont("Segoe UI", 13)
        version_font.setWeight(QFont.Medium)
        version.setFont(version_font)

        text_layout.addWidget(title)
        text_layout.addWidget(version)
        layout.addWidget(logo, 0, Qt.AlignVCenter)
        layout.addWidget(text_wrap, 1, Qt.AlignVCenter)

    def center_on_primary_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        rect = screen.availableGeometry()
        self.move(
            rect.x() + (rect.width() - self.width()) // 2,
            rect.y() + (rect.height() - self.height()) // 2,
        )


class PopupPage(QWebEnginePage):
    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        self.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
        # QtWebEngine shows no native prompt: it emits this signal and waits
        # for an answer, so a request left unconnected stays pending forever.
        self.featurePermissionRequested.connect(self._on_feature_permission)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        print(f"[JS] {source_id}:{line_number} {message}")

    def _on_feature_permission(self, security_origin, feature):
        # Nothing in the UI needs camera, microphone, geolocation or
        # notifications, and QtWebEngine waits forever for an answer instead of
        # showing a prompt -- so answer, and answer no.
        self.setFeaturePermission(security_origin, feature, QWebEnginePage.PermissionDeniedByUser)

    def createWindow(self, _type):
        view = QWebEngineView()
        if APP_ICON is not None:
            view.setWindowIcon(APP_ICON)
        view.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        view.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
        page = PopupPage(self.profile(), view)
        view.setPage(page)
        view.resize(900, 700)
        view.setWindowTitle(f"{app_display_name()} - Popup")
        view.setAttribute(Qt.WA_DeleteOnClose, True)
        view.show()
        if os.path.exists(ICON_ICO_PATH):
            set_taskbar_icon(view, ICON_ICO_PATH)
        _popup_views.append(view)
        view.destroyed.connect(lambda: _popup_views.remove(view) if view in _popup_views else None)
        return page


if __name__ == "__main__":
    qt_app = QApplication(sys.argv)
    set_windows_app_id("DummyDMX")
    if ensure_ico(ICON_SVG_PATH, ICON_ICO_PATH):
        APP_ICON = QIcon(ICON_ICO_PATH)
    else:
        APP_ICON = build_svg_icon(ICON_SVG_PATH)
    splash_logo = build_svg_pixmap(ICON_SVG_PATH, 96)
    splash = StartupSplash(splash_logo)
    # Settle the port here: the splash and the web view read URL/PORT while the
    # server thread is still booting.
    chosen = choose_port()
    if chosen != PREFERRED_PORT:
        print(f"[GUI] port {PREFERRED_PORT} is taken; serving on {chosen} instead")
    threading.Thread(target=start_server, daemon=True).start()
    if APP_ICON is not None:
        splash.setWindowIcon(APP_ICON)
    splash.center_on_primary_screen()
    splash.show()
    server_ok, server_error = wait_for_server_ready()
    if not server_ok:
        splash.close()
        QMessageBox.critical(None, app_display_name(), f"Backend startup failed.\n\n{server_error}")
        stop_server()
        sys.exit(1)
    auto_update_settings = SETTINGS.get("auto_update") or {}
    if bool(auto_update_settings.get("check_on_startup", True)):
        threading.Thread(target=check_for_updates, kwargs={"manual": False}, daemon=True).start()
    time.sleep(GUI_POST_READY_DELAY_SEC)
    if APP_ICON is not None:
        qt_app.setWindowIcon(APP_ICON)
    qt_app.aboutToQuit.connect(stop_server)
    quit_timer = QTimer()
    quit_timer.setInterval(150)

    def check_requested_quit():
        if REQUESTED_APP_QUIT.is_set():
            REQUESTED_APP_QUIT.clear()
            qt_app.quit()

    quit_timer.timeout.connect(check_requested_quit)
    quit_timer.start()
    view = QWebEngineView()
    view.setStyleSheet("background-color: #000000;")
    view.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
    view.settings().setAttribute(QWebEngineSettings.AutoLoadImages, True)
    view.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
    view.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
    page = PopupPage(view.page().profile(), view)
    page.setBackgroundColor(QColor("#000000"))
    page.profile().clearHttpCache()
    view.setPage(page)
    view.resize(1280, 800)
    view.setWindowTitle(app_display_name())
    if APP_ICON is not None:
        view.setWindowIcon(APP_ICON)

    def load_url(attempt=0):
        view.load(QUrl(f"{URL}?gui=1&t={int(time.time())}&attempt={attempt}"))

    def retry_load(reason):
        attempt = int(view.property("load_attempt") or 0) + 1
        view.setProperty("load_attempt", attempt)
        if attempt <= MAX_LOAD_ATTEMPTS:
            QTimer.singleShot(LOAD_RETRY_MS, lambda: load_url(attempt))
        else:
            print(f"[GUI] load failed after retries: {reason}")

    def force_startup_reload():
        if bool(view.property("startup_force_reload_done")):
            return
        view.setProperty("startup_force_reload_done", True)
        attempt = int(view.property("load_attempt") or 0) + 1
        view.setProperty("load_attempt", attempt)
        print("[GUI] forcing startup reload")
        load_url(attempt)

    def on_load_finished(ok):
        if not ok:
            retry_load("loadFinished=false")
            return

        def check_dom(result):
            if result:
                def finish_startup():
                    if splash.isVisible():
                        splash.close()
                    if not view.isVisible():
                        view.show()
                        if os.path.exists(ICON_ICO_PATH):
                            set_taskbar_icon(view, ICON_ICO_PATH)
                QTimer.singleShot(GUI_SPLASH_MIN_VISIBLE_MS, finish_startup)
                return
            retry_load("dom_missing")

        QTimer.singleShot(300, lambda: page.runJavaScript("!!document.getElementById('rig-canvas')", check_dom))

    view.loadFinished.connect(on_load_finished)
    load_url()
    QTimer.singleShot(STARTUP_FORCE_RELOAD_MS, force_startup_reload)
    sys.exit(qt_app.exec())
