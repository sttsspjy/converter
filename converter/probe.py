"""Media inspection via ffprobe, plus small thumbnails for the queue."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .tools import TOOLS, run

VIDEO_EXTS = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.wmv', '.flv', '.ts', '.mts', '.m2ts', '.3gp', '.mpg', '.mpeg', '.ogv'}
AUDIO_EXTS = {'.flac', '.wav', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.alac', '.aiff', '.aif', '.ape', '.wv', '.dsf', '.mka'}
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.avif', '.heic', '.jxl'}
GIF_EXTS = {'.gif'}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | GIF_EXTS

_IMAGE_FORMATS = ('_pipe', 'image2', 'webp', 'bmp', 'tiff', 'avif')


class ProbeError(Exception):
    pass


def _fps(rate: str | None) -> float | None:
    if not rate or rate in ('0/0', '0'):
        return None
    try:
        n, _, d = rate.partition('/')
        v = float(n) / float(d or 1)
        return round(v, 3) if 0 < v < 1000 else None
    except (ValueError, ZeroDivisionError):
        return None


def _rotation(stream: dict) -> int:
    for sd in stream.get('side_data_list') or []:
        if 'rotation' in sd:
            try:
                return int(float(sd['rotation'])) % 360
            except ValueError:
                pass
    try:
        return int(stream.get('tags', {}).get('rotate', 0)) % 360
    except ValueError:
        return 0


def _bit_depth(stream: dict) -> int:
    raw = stream.get('bits_per_raw_sample')
    if raw and str(raw).isdigit() and int(raw) > 0:
        return int(raw)
    pf = stream.get('pix_fmt') or ''
    for d in (16, 12, 10):
        if str(d) in pf:
            return d
    return 8


def probe(path: str) -> dict:
    p = Path(path)
    r = run([TOOLS.ffprobe, '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', str(p)], timeout=60)
    if r.returncode != 0:
        msg = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise ProbeError(msg[-1] if msg else 'Not a media file ffmpeg can read')
    data = json.loads(r.stdout.decode('utf-8', 'replace') or '{}')
    fmt = data.get('format', {})
    streams = data.get('streams', [])
    fmt_name = fmt.get('format_name', '')

    video = next((s for s in streams if s.get('codec_type') == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    cover = next((s for s in streams if s.get('codec_type') == 'video'
                  and s.get('disposition', {}).get('attached_pic')), None)
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    audio = audios[0] if audios else None

    try:
        duration = float(fmt.get('duration') or 0)
    except ValueError:
        duration = 0.0
    if not duration and video:
        try:
            duration = float(video.get('duration') or 0)
        except ValueError:
            pass

    ext = p.suffix.lower()
    if 'gif' in fmt_name or ext in GIF_EXTS:
        kind = 'gif'
    elif video and (ext in IMAGE_EXTS or any(k in fmt_name for k in _IMAGE_FORMATS)) and not audio:
        kind = 'image'
    elif video:
        kind = 'video'
    elif audio:
        kind = 'audio'
    else:
        raise ProbeError('No audio or video found in this file')

    info: dict = {
        'kind': kind,
        'size': p.stat().st_size,
        'duration': duration,
        'format': fmt_name,
        'bitrate': int(fmt.get('bit_rate') or 0) or None,
        'tags': {k.lower(): v for k, v in (fmt.get('tags') or {}).items()},
        'has_cover': cover is not None,
        'cover_index': cover['index'] if cover else None,
        'audio_tracks': len(audios),
        'audio_codecs': [a.get('codec_name') for a in audios],
        'has_subs': any(s.get('codec_type') == 'subtitle' for s in streams),
    }

    if video:
        w, h = int(video.get('width') or 0), int(video.get('height') or 0)
        rot = _rotation(video)
        if rot in (90, 270):
            w, h = h, w
        info.update({
            'video_index': video['index'],
            'vcodec': video.get('codec_name'),
            'width': w, 'height': h,
            'fps': _fps(video.get('avg_frame_rate')) or _fps(video.get('r_frame_rate')),
            'bit_depth': _bit_depth(video),
            'pix_fmt': video.get('pix_fmt'),
            'hdr': video.get('color_transfer') in ('smpte2084', 'arib-std-b67'),
            'frames': int(video['nb_frames']) if str(video.get('nb_frames', '')).isdigit() else None,
        })
    if audio:
        info.update({
            'acodec': audio.get('codec_name'),
            'sample_rate': int(audio.get('sample_rate') or 0) or None,
            'channels': audio.get('channels'),
            'abitrate': int(audio.get('bit_rate') or 0) or None,
            'audio_bits': int(audio.get('bits_per_raw_sample') or 0) or None,
        })
        # Ogg/FLAC keep tags on the stream rather than the container.
        if not info['tags']:
            info['tags'] = {k.lower(): v for k, v in (audio.get('tags') or {}).items()}
    return info


def thumbnail(path: str, info: dict, height: int = 120) -> str | None:
    """Return a small JPEG as a data URI, or None."""
    kind = info.get('kind')
    args = [TOOLS.ffmpeg, '-hide_banner', '-v', 'error', '-nostdin']
    if kind == 'video' and info.get('duration', 0) > 2:
        args += ['-ss', f"{info['duration'] * 0.2:.2f}"]
    args += ['-i', path]
    if kind == 'audio':
        if not info.get('has_cover'):
            return None
        args += ['-map', f"0:{info['cover_index']}"]
    else:
        args += ['-map', f"0:{info.get('video_index', 'v:0')}"]
    args += ['-frames:v', '1', '-vf', f'scale=-2:{height}:flags=bicubic',
             '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '4', '-']
    try:
        r = run(args, timeout=30)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return 'data:image/jpeg;base64,' + base64.b64encode(r.stdout).decode()


def frame_at(path: str, t: float, height: int = 360) -> str | None:
    """A single frame for the trim scrubber when the webview can't play the file."""
    args = [TOOLS.ffmpeg, '-hide_banner', '-v', 'error', '-nostdin', '-ss', f'{max(0, t):.3f}',
            '-i', path, '-frames:v', '1', '-vf', f'scale=-2:{height}',
            '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '5', '-']
    try:
        r = run(args, timeout=20)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return 'data:image/jpeg;base64,' + base64.b64encode(r.stdout).decode()
