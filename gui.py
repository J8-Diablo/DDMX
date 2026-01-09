import os
import socket
import sys
import threading
import time
import urllib.request
import ctypes
from ctypes import wintypes

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
try:
    from PySide6.QtSvg import QSvgRenderer
except ImportError:
    QSvgRenderer = None
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

from app import app, init_engine, setup_engine_callbacks


HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}/"
_popup_views = []
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_SVG_PATH = os.path.join(BASE_DIR, "static", "favicon.svg")
ICON_ICO_PATH = os.path.join(BASE_DIR, "static", "favicon.ico")
APP_ICON = None


def start_server() -> None:
    init_engine()
    setup_engine_callbacks()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


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
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    data = resp.read(4096)
                    if b"<html" in data or b"<!doctype" in data.lower():
                        return True
        except Exception:
            time.sleep(0.2)
    return False


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


class PopupPage(QWebEnginePage):
    def __init__(self, profile, parent=None):
        super().__init__(profile, parent)
        self.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        self.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        print(f"[JS] {source_id}:{line_number} {message}")

    def createWindow(self, _type):
        view = QWebEngineView()
        if APP_ICON is not None:
            view.setWindowIcon(APP_ICON)
        view.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
        view.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
        page = PopupPage(self.profile(), view)
        view.setPage(page)
        view.resize(900, 700)
        view.setWindowTitle("DDMX - Popup")
        view.setAttribute(Qt.WA_DeleteOnClose, True)
        view.show()
        if os.path.exists(ICON_ICO_PATH):
            set_taskbar_icon(view, ICON_ICO_PATH)
        _popup_views.append(view)
        view.destroyed.connect(lambda: _popup_views.remove(view) if view in _popup_views else None)
        return page


if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    wait_for_port(HOST, PORT)
    wait_for_http(URL)

    qt_app = QApplication(sys.argv)
    set_windows_app_id("DummyDMX")
    if ensure_ico(ICON_SVG_PATH, ICON_ICO_PATH):
        APP_ICON = QIcon(ICON_ICO_PATH)
    else:
        APP_ICON = build_svg_icon(ICON_SVG_PATH)
    if APP_ICON is not None:
        qt_app.setWindowIcon(APP_ICON)
    view = QWebEngineView()
    view.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
    view.settings().setAttribute(QWebEngineSettings.AutoLoadImages, True)
    view.settings().setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
    view.settings().setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
    page = PopupPage(view.page().profile(), view)
    page.profile().clearHttpCache()
    view.setPage(page)
    view.resize(1280, 800)
    view.setWindowTitle("DDMX")
    if APP_ICON is not None:
        view.setWindowIcon(APP_ICON)

    def load_url(attempt=0):
        view.load(QUrl(f"{URL}?t={int(time.time())}&attempt={attempt}"))

    def retry_load(reason):
        attempt = int(view.property("load_attempt") or 0) + 1
        view.setProperty("load_attempt", attempt)
        if attempt <= 20:
            QTimer.singleShot(500, lambda: load_url(attempt))
        else:
            print(f"[GUI] load failed after retries: {reason}")

    def on_load_finished(ok):
        if not ok:
            retry_load("loadFinished=false")
            return

        def check_dom(result):
            if result:
                return
            retry_load("dom_missing")

        page.runJavaScript("!!document.getElementById('rig-canvas')", check_dom)

    view.loadFinished.connect(on_load_finished)
    load_url()
    view.show()
    if os.path.exists(ICON_ICO_PATH):
        set_taskbar_icon(view, ICON_ICO_PATH)
    sys.exit(qt_app.exec())
