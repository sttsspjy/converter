"""One-click download of the external tools into %LOCALAPPDATA%/Converter/tools.

Every archive is pinned to an exact version and SHA-256, so a changed or
tampered download is rejected rather than run.
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import urllib.request
import zipfile
from pathlib import Path

from . import __version__
from .paths import TOOLS_DIR
from .tools import TOOLS

SOURCES = {
    'ffmpeg': {
        'label': 'ffmpeg 9.0.2 (gyan.dev full build)',
        'url': 'https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-full_build-shared.zip',
        'sha256': '8d31e162f1616e37aab3fa2db991b97e1b8dbeb1c8465fd81a81be16ff91b328',
        'size': 99_000_000,
        # Shared build: the exes need the DLLs beside them. ffplay isn't used.
        'keep': lambda name: name.endswith('.dll') or name in ('ffmpeg.exe', 'ffprobe.exe'),
        'license': 'LICENSE',
        'gives': ('ffmpeg', 'ffprobe'),
    },
    'opusenc': {
        'label': 'opus-tools 0.2 (Xiph.Org)',
        'url': 'https://archive.mozilla.org/pub/opus/win32/opus-tools-0.2-opus-1.3.zip',
        'sha256': 'a1ae3c806adee9b008348166251f938dd7774ba6987d392187202b11d1152e90',
        'size': 1_050_000,
        'keep': lambda name: name == 'opusenc.exe',
        'license': 'LICENSE',
        'gives': ('opusenc',),
    },
    'gifsicle': {
        'label': 'gifsicle 1.95',
        'url': 'https://eternallybored.org/misc/gifsicle/releases/gifsicle-1.95-win64.zip',
        'sha256': '7e47dd0bfd5ee47f911464c57faeed89a8709a7625dd1c449b16579889539ee8',
        'size': 230_000,
        'keep': lambda name: name == 'gifsicle.exe',
        'license': 'COPYING',
        'gives': ('gifsicle',),
    },
}


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, on_done=None):
        self.lock = threading.Lock()
        self.on_done = on_done or (lambda: None)
        self.state = {'active': None, 'done': 0, 'total': 0, 'error': None, 'failed': None, 'queue': []}
        self._cancel = False

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state, queue=list(self.state['queue']))

    def start(self, names: list[str]) -> bool:
        names = [n for n in names if n in SOURCES]
        with self.lock:
            if self.state['active'] or not names:
                return False
            self._cancel = False
            self.state = {'active': names[0], 'done': 0, 'total': 0, 'error': None, 'failed': None, 'queue': names}
        threading.Thread(target=self._run, args=(names,), daemon=True, name='fetch').start()
        return True

    def cancel(self):
        self._cancel = True

    def _run(self, names):
        try:
            for name in names:
                with self.lock:
                    self.state.update(active=name, done=0, total=SOURCES[name]['size'])
                self._fetch(name)
                with self.lock:
                    self.state['queue'] = [n for n in self.state['queue'] if n != name]
            with self.lock:
                self.state.update(active=None, error=None)
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, FetchError) else f'Download failed: {e}'
            with self.lock:
                self.state.update(active=None, error=msg, failed=self.state['active'], queue=[])
        finally:
            TOOLS.refresh()
            self.on_done()

    def _fetch(self, name: str):
        src = SOURCES[name]
        staging = TOOLS_DIR / f'.{name}-download'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        archive = staging / 'archive.zip'
        try:
            req = urllib.request.Request(src['url'], headers={'User-Agent': f'Converter/{__version__}'})
            h = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=30) as resp, open(archive, 'wb') as out:
                total = int(resp.headers.get('Content-Length') or src['size'])
                with self.lock:
                    self.state['total'] = total
                while True:
                    if self._cancel:
                        raise FetchError('Download cancelled')
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    out.write(chunk)
                    h.update(chunk)
                    with self.lock:
                        self.state['done'] += len(chunk)
            if h.hexdigest() != src['sha256']:
                raise FetchError(f'{src["label"]}: the download didn’t match its checksum, so it was discarded')

            extracted = staging / 'files'
            extracted.mkdir()
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    base = Path(info.filename).name
                    if info.is_dir() or not base:
                        continue
                    if src['keep'](base.lower()) or base == src['license']:
                        target = extracted / (f'{name}-{base}' if base == src['license'] else base)
                        with z.open(info) as fin, open(target, 'wb') as fout:
                            shutil.copyfileobj(fin, fout)
            wanted = [g + '.exe' for g in src['gives']]
            if not all((extracted / w).exists() for w in wanted):
                raise FetchError(f'{src["label"]}: the archive didn’t contain {", ".join(wanted)}')
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            for f in extracted.iterdir():
                f.replace(TOOLS_DIR / f.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def missing_tools() -> list[str]:
    out = []
    if not (TOOLS.ffmpeg and TOOLS.ffprobe):
        out.append('ffmpeg')
    if not TOOLS.opusenc:
        out.append('opusenc')
    return out
