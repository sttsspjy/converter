"""Converter: a desktop front-end for ffmpeg / opusenc.

Run with:  .venv\\Scripts\\pythonw.exe app.py [files or folders…]
If an instance is already open, the paths are handed to it instead.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from converter.paths import LOCAL_DIR, RES_DIR  # noqa: E402

LOCAL_DIR.mkdir(parents=True, exist_ok=True)
LOCK = LOCAL_DIR / 'instance.json'

if sys.stderr is None or sys.stdout is None:   # pythonw / windowed exe: no console
    _log = open(LOCAL_DIR / 'converter.log', 'a', encoding='utf-8', buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')


def forward_to_running(paths: list[str]) -> bool:
    try:
        meta = json.loads(LOCK.read_text('utf-8'))
        with socket.create_connection(('127.0.0.1', meta['port']), timeout=1.5) as s:
            s.sendall(json.dumps({'token': meta['token'], 'paths': paths}).encode('utf-8'))
            s.shutdown(socket.SHUT_WR)
            return s.recv(16) == b'ok'
    except Exception:
        return False


def listen_for_instances(on_paths) -> socket.socket:
    token = secrets.token_hex(16)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(('127.0.0.1', 0))
    srv.listen(4)
    LOCK.write_text(json.dumps({'port': srv.getsockname()[1], 'token': token, 'pid': os.getpid()}), 'utf-8')

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(3)
                    data = b''
                    while chunk := conn.recv(65536):
                        data += chunk
                    msg = json.loads(data.decode('utf-8'))
                    if msg.get('token') == token:
                        conn.sendall(b'ok')
                        on_paths(msg.get('paths') or [])
                except Exception:
                    pass
    threading.Thread(target=loop, daemon=True, name='instance').start()
    return srv


def system_dark() -> bool:
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        return winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] == 0
    except Exception:
        return True


def main():
    paths = [os.path.abspath(a) for a in sys.argv[1:] if not a.startswith('--')]
    if forward_to_running(paths):
        return

    if sys.platform == 'win32':
        import ctypes
        # Own taskbar identity, so Windows shows our icon instead of Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Converter.MediaConverter')
        # Lets the installer notice a running copy before replacing files.
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'ConverterAppMutex')  # noqa: F841

    import webview
    from webview.dom import DOMEventHandler
    from converter.api import APP_TITLE, Api

    api = Api()
    dark = system_dark()
    window = webview.create_window(
        APP_TITLE, url=str(RES_DIR / 'ui' / 'index.html'), js_api=api,
        width=1220, height=800, min_size=(960, 640),
        background_color='#16191f' if dark else '#eef0f3', text_select=False,
    )
    api._bind(window)
    window.events.closing += api._on_closing

    def push_added(result):
        try:
            window.evaluate_js(f'window.onFilesAdded && window.onFilesAdded({json.dumps(result)})')
        except Exception:
            pass

    def on_drop(e):
        files = (e.get('dataTransfer') or {}).get('files') or []
        dropped = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
        push_added(api.add_paths(dropped) if dropped else {'added': 0, 'skipped': len(files)})


    def on_forwarded(more):
        push_added(api.add_paths(more))
        try:
            window.restore()
            hwnd = api._hwnd()
            if hwnd:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    hooked = threading.Event()

    def on_loaded():
        if hooked.is_set():
            return
        hooked.set()
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        if paths:
            push_added(api.add_paths(paths))

    window.events.loaded += on_loaded
    srv = listen_for_instances(on_forwarded)

    webview.start(debug=os.environ.get('CONVERTER_DEBUG') == '1', icon=str(RES_DIR / 'assets' / 'icon.ico'),
                  storage_path=str(LOCAL_DIR / 'webview'), private_mode=False)

    # Window closed: give cancelled jobs a moment to delete their partial files.
    srv.close()
    try:
        LOCK.unlink(missing_ok=True)
    except OSError:
        pass
    deadline = time.time() + 4
    while api._queue.busy() and time.time() < deadline:
        time.sleep(0.1)


if __name__ == '__main__':
    main()
