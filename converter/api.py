"""Methods the web UI calls through pywebview's js_api bridge."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import threading
import urllib.request
import webbrowser
from pathlib import Path

import webview

from . import REPO, __version__, tasks
from .fetch import SOURCES, Fetcher, missing_tools
from .jobs import Queue
from .mediaserver import MediaServer
from .probe import MEDIA_EXTS, frame_at
from .settings import DEFAULTS, Settings
from .tools import IS_WIN, TOOLS

APP_TITLE = 'Converter'


class Api:
    def __init__(self):
        self._settings = Settings()
        self._window: webview.Window | None = None
        self._queue = Queue(self._settings, on_idle=self._on_idle)
        self._media = MediaServer(lambda jid: (self._queue.get(jid).path if self._queue.get(jid) else None))
        self._last_title = APP_TITLE
        self._fetcher = Fetcher(on_done=self._tools_changed)
        self._tools_rev = 0
        threading.Thread(target=TOOLS.detect_hw, daemon=True).start()

    def _bind(self, window):
        self._window = window

    # ── bootstrap ──
    def init(self) -> dict:
        return {
            'settings': self._settings.get(),
            'defaults': DEFAULTS,
            'quality_names': tasks.QUALITY_NAMES,
            'version': __version__,
            'repo': REPO,
            **self.tools_info(),
        }

    # ── external tools ──
    def tools_info(self) -> dict:
        return {
            'caps': TOOLS.caps(),
            'missing': missing_tools(),
            'sources': {k: {'label': v['label'], 'size': v['size']} for k, v in SOURCES.items()},
            'tools_rev': self._tools_rev,
        }

    def fetch_tools(self, names: list) -> bool:
        return self._fetcher.start(list(names))

    def cancel_fetch(self):
        self._fetcher.cancel()

    def _tools_changed(self):
        TOOLS._hw_done.wait(timeout=30)
        self._tools_rev += 1

    def check_update(self) -> dict | None:
        """Newer GitHub release, if any. Silent on any failure (offline etc.)."""
        try:
            req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/releases/latest',
                                         headers={'User-Agent': f'Converter/{__version__}',
                                                  'Accept': 'application/vnd.github+json'})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode('utf-8'))
            tag = str(data.get('tag_name', '')).lstrip('v')

            def key(v):
                return tuple(int(x) if x.isdigit() else 0 for x in v.split('.')[:3])
            if tag and key(tag) > key(__version__):
                return {'version': tag, 'url': data.get('html_url')}
        except Exception:
            pass
        return None

    def open_url(self, url: str):
        allowed = (f'https://github.com/{REPO}', 'https://ffmpeg.org', 'https://opus-codec.org')
        if isinstance(url, str) and url.startswith(allowed):
            webbrowser.open(url)

    # ── queue ──
    def poll(self, since: int = 0) -> dict:
        snap = self._queue.snapshot(since)
        snap['fetch'] = self._fetcher.snapshot()
        snap['tools_rev'] = self._tools_rev
        self._update_title()
        return snap

    def add_paths(self, paths: list) -> dict:
        if not (TOOLS.ffmpeg and TOOLS.ffprobe):
            return {'added': 0, 'skipped': 0, 'needs_ffmpeg': True}
        return self._queue.add_paths([p for p in paths if p])

    def pick_files(self) -> dict | None:
        exts = ';'.join(f'*{e}' for e in sorted(MEDIA_EXTS))
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            file_types=(f'Media files ({exts})', 'All files (*.*)'))
        return self.add_paths(list(res)) if res else None

    def pick_folder(self) -> dict | None:
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return self.add_paths(list(res)) if res else None

    def choose_output_folder(self) -> dict | None:
        current = self._settings.section('output')
        res = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=current.get('folder') or '')
        if not res:
            return None
        folder = res[0] if isinstance(res, (list, tuple)) else res
        recent = [folder] + [r for r in current.get('recent', []) if r.lower() != folder.lower()]
        return self._settings.update({'output': {'mode': 'folder', 'folder': folder, 'recent': recent[:5]}})

    def set_task(self, job_id: str, task: str):
        self._queue.set_task(job_id, task)

    def set_trim(self, job_id: str, start, end):
        self._queue.set_trim(job_id, start, end)

    def remove(self, ids: list):
        self._queue.remove(ids)

    def clear(self, which: str = 'finished'):
        self._queue.clear(which)

    def start(self, ids: list | None = None) -> int:
        return self._queue.start(ids)

    def cancel(self, ids: list | None = None):
        self._queue.cancel(ids)

    # ── settings / preview ──
    def save_settings(self, patch: dict) -> dict:
        return self._settings.update(patch)

    def preview(self) -> dict:
        """Per-job plan summaries for the current settings (ready jobs only)."""
        s = self._settings.get()
        caps = TOOLS.caps()
        out = {}
        with self._queue.lock:
            jobs = list(self._queue.jobs.values())
        for job in jobs:
            if job.status not in ('ready', 'error', 'cancelled') or not job.task or not job.info:
                continue
            try:
                plan = tasks.PLANNERS[job.task](job.info, s[job.task], job.trim, caps)
                out[job.id] = {'summary': plan['summary'], 'estimate': plan.get('estimate')}
            except tasks.PlanError as e:
                out[job.id] = {'problem': str(e)}
            except Exception as e:  # noqa: BLE001
                out[job.id] = {'problem': str(e)}
        return out

    # ── files ──
    def reveal(self, path: str):
        if path and os.path.exists(path):
            if IS_WIN:
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
            else:
                subprocess.Popen(['xdg-open', str(Path(path).parent)])

    def open_path(self, path: str):
        if path and os.path.exists(path):
            os.startfile(path) if IS_WIN else subprocess.Popen(['xdg-open', path])

    def media_url(self, job_id: str) -> str:
        return self._media.url(job_id)

    def frame(self, job_id: str, t: float) -> str | None:
        job = self._queue.get(job_id)
        return frame_at(job.path, float(t)) if job else None

    # ── window chrome ──
    def _update_title(self):
        active, total, frac = self._queue.overall()
        title = f'{round(frac * 100)}% · {active} left · {APP_TITLE}' if active else APP_TITLE
        if title != self._last_title and self._window:
            self._last_title = title
            try:
                self._window.set_title(title)
            except Exception:
                pass

    def _hwnd(self) -> int | None:
        try:
            return int(self._window.native.Handle.ToInt64())
        except Exception:
            return None

    def _on_idle(self):
        if not IS_WIN:
            return
        hwnd = self._hwnd()
        if not hwnd or ctypes.windll.user32.GetForegroundWindow() == hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('hwnd', ctypes.c_void_p), ('dwFlags', ctypes.c_uint),
                        ('uCount', ctypes.c_uint), ('dwTimeout', ctypes.c_uint)]
        FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))

    def _on_closing(self):
        if not self._queue.busy():
            return True
        msg = 'Files are still converting. Quit and cancel them?\n\nPartly written files are deleted.'
        if IS_WIN:
            MB_OKCANCEL, MB_ICONWARNING, IDOK = 0x1, 0x30, 1
            ok = ctypes.windll.user32.MessageBoxW(self._hwnd() or 0, msg, APP_TITLE,
                                                  MB_OKCANCEL | MB_ICONWARNING) == IDOK
        else:
            ok = True
        if not ok:
            return False
        self._queue.kill_all()
        return True
