"""Locating external tools and running them without console windows."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from .paths import APP_DIR, TOOLS_DIR

IS_WIN = os.name == 'nt'
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY = 0x00004000

# Encoders the tasks rely on; reported to the UI so it can explain gaps.
WANTED_ENCODERS = ('libx264', 'libx265', 'libsvtav1', 'libwebp_anim', 'libopus', 'libmp3lame', 'aac', 'flac', 'mjpeg')

# Common hand-install locations, checked after PATH.
_EXTRA_DIRS = {
    'opusenc': [r'C:\opusenc', r'C:\Program Files\opus-tools'],
    'gifsicle': [r'C:\ProgramData\chocolatey\bin', r'C:\Program Files\gifsicle'],
    'ffmpeg': [r'C:\ffmpeg', r'C:\ffmpeg\bin'],
    'ffprobe': [r'C:\ffmpeg', r'C:\ffmpeg\bin'],
}


def find_tool(name: str) -> str | None:
    """Downloaded copy first (known-good), then ./bin, then PATH, then common folders."""
    exe = name + ('.exe' if IS_WIN else '')
    for d in (TOOLS_DIR, APP_DIR / 'bin'):
        p = Path(d) / exe
        if p.is_file():
            return str(p)
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_DIRS.get(name, []):
        p = Path(d) / exe
        if p.is_file():
            return str(p)
    return None


class Tools:
    def __init__(self):
        self.ffmpeg = self.ffprobe = self.opusenc = self.gifsicle = None
        self.nvenc: dict[str, bool] = {}
        self.encoders: set[str] = set()
        self._hw_done = threading.Event()
        self.refresh(probe_hw=False)

    def refresh(self, probe_hw: bool = True):
        self.ffmpeg = find_tool('ffmpeg')
        self.ffprobe = find_tool('ffprobe')
        self.opusenc = find_tool('opusenc')
        self.gifsicle = find_tool('gifsicle')
        if probe_hw:
            self._hw_done.clear()
            threading.Thread(target=self.detect_hw, daemon=True).start()

    def detect_hw(self):
        """List encoders, then test-encode a tiny clip with each NVENC encoder.
        Listing alone isn't enough: NVENC shows up in builds without an NVIDIA GPU."""
        nvenc, encoders = {}, set()
        try:
            if not self.ffmpeg:
                return
            r = run([self.ffmpeg, '-hide_banner', '-encoders'], timeout=20)
            for line in r.stdout.decode('utf-8', 'replace').splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] in WANTED_ENCODERS:
                    encoders.add(parts[1])
            for codec, enc in (('h264', 'h264_nvenc'), ('hevc', 'hevc_nvenc'), ('av1', 'av1_nvenc')):
                r = run([self.ffmpeg, '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i',
                         'color=s=256x256:d=0.2', '-c:v', enc, '-f', 'null', '-'], timeout=20)
                nvenc[codec] = r.returncode == 0
        except Exception:
            pass
        finally:
            self.nvenc, self.encoders = nvenc, encoders
            self._hw_done.set()

    def caps(self) -> dict:
        self._hw_done.wait(timeout=15)
        return {
            'ffmpeg': bool(self.ffmpeg and self.ffprobe),
            'opusenc': bool(self.opusenc),
            'gifsicle': bool(self.gifsicle),
            'nvenc': self.nvenc,
            'encoders': sorted(self.encoders),
            'missing_encoders': [e for e in WANTED_ENCODERS if self.encoders and e not in self.encoders],
        }


def popen_flags(low_priority: bool = False) -> int:
    if not IS_WIN:
        return 0
    flags = CREATE_NO_WINDOW
    if low_priority:
        flags |= BELOW_NORMAL_PRIORITY
    return flags


def run(args, timeout=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, timeout=timeout, cwd=cwd,
        creationflags=popen_flags(), stdin=subprocess.DEVNULL,
    )


TOOLS = Tools()
