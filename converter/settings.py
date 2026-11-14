"""User settings, persisted as JSON in %APPDATA%/Converter."""
from __future__ import annotations

import copy
import json
import os
import threading

from .paths import DATA_DIR

DEFAULTS: dict = {
    'video': {
        'codec': 'hevc',          # hevc | h264 | av1
        'encoder': 'gpu',         # gpu | cpu  (gpu falls back to cpu when NVENC is missing)
        'mode': 'quality',        # quality | size
        'quality': 2,             # 0 near-lossless … 4 smallest
        'target_mb': 10,
        'auto_scale': True,       # size mode: lower resolution when the bitrate would be starved
        'resolution': 0,          # 0 = keep; else the short side in px (1080, 720, …)
        'fps': 0,                 # 0 = keep; else a cap
        'speed': 'balanced',      # fast | balanced | small
        'audio': 'auto',          # auto | aac | opus | none
        'audio_kbps': 128,
        'container': 'mp4',       # mp4 | mkv
    },
    'gif': {
        'format': 'gif',          # gif | webp | mp4
        'max_side': 0,            # 0 = keep; else longest side in px
        'fps': 0,
        'colors': 256,
        'dither': 'sierra2_4a',   # sierra2_4a | bayer | none
        'lossy': 60,              # gifsicle --lossy, only when gifsicle exists
        'quality': 75,            # webp / mp4
        'limit_on': False,
        'limit_mb': 10,
    },
    'audio': {
        'format': 'opus',         # opus | mp3 | aac | flac
        'opus_kbps': 128,
        'mp3_kbps': 320,
        'aac_kbps': 256,
        'cover': 'keep',          # keep | shrink | remove
    },
    'image': {
        'format': 'webp',         # webp | avif | jpg | png
        'quality': 82,
        'max_side': 0,
    },
    'output': {
        'mode': 'beside',         # beside | folder
        'folder': '',
        'copy_art': True,         # copy cover.jpg & co. when mirroring album folders
        'conflict': 'rename',     # rename | skip | overwrite  (the source itself is never overwritten)
        'recent': [],
    },
    'general': {
        'low_priority': True,
        'keep_awake': True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


class Settings:
    def __init__(self):
        self._lock = threading.Lock()
        self.path = DATA_DIR / 'settings.json'
        try:
            saved = json.loads(self.path.read_text('utf-8'))
        except Exception:
            saved = {}
        self.data = _merge(DEFAULTS, saved)

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def section(self, name: str) -> dict:
        with self._lock:
            return copy.deepcopy(self.data[name])

    def update(self, patch: dict) -> dict:
        with self._lock:
            self.data = _merge(self.data, patch)
            self._save()
            return copy.deepcopy(self.data)

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), 'utf-8')
            os.replace(tmp, self.path)
        except OSError:
            pass
