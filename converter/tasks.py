"""What each task does.

Every task exposes two functions:
  plan(info, settings, trim, caps) -> dict   cheap; drives the UI preview line
  execute(ctx)                               does the work through a JobContext

`plan` never touches the disk, so it is safe to call on every settings change.
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path

from .tools import TOOLS

TASKS = ('video', 'gif', 'audio', 'image')
QUALITY_NAMES = ['Near lossless', 'High', 'Balanced', 'Small', 'Smallest']

# CRF / CQ per quality step (index 0 = best). Tuned so each codec's "Balanced"
# looks about the same, which means AV1 < HEVC < H.264 in size.
CRF = {
    ('h264', 'cpu'): [17, 20, 23, 26, 30],
    ('hevc', 'cpu'): [19, 22, 25, 28, 32],
    ('av1', 'cpu'): [22, 28, 34, 40, 47],
    ('h264', 'gpu'): [18, 21, 24, 28, 32],
    ('hevc', 'gpu'): [20, 24, 27, 31, 35],
    ('av1', 'gpu'): [22, 28, 33, 39, 46],
}
SPEED = {
    'x26x': {'fast': 'fast', 'balanced': 'medium', 'small': 'slow'},
    'svt': {'fast': '8', 'balanced': '6', 'small': '4'},
    'nvenc': {'fast': 'p4', 'balanced': 'p6', 'small': 'p7'},
}
NVENC_NAME = {'h264': 'h264_nvenc', 'hevc': 'hevc_nvenc', 'av1': 'av1_nvenc'}
CODEC_LABEL = {'h264': 'H.264', 'hevc': 'HEVC', 'av1': 'AV1'}
# Bits per pixel per frame below which a codec starts to visibly fall apart.
MIN_BPP = {'h264': 0.055, 'hevc': 0.038, 'av1': 0.032}
SHORT_LADDER = [2160, 1440, 1080, 720, 540, 480, 360, 240]
MP4_AUDIO_OK = {'aac', 'mp3', 'ac3', 'eac3', 'alac', 'opus', 'flac'}
DIRECT_OPUSENC = {'.flac', '.wav', '.aif', '.aiff'}

AUDIO_EXT = {'opus': '.opus', 'mp3': '.mp3', 'aac': '.m4a', 'flac': '.flac'}
IMAGE_EXT = {'webp': '.webp', 'avif': '.avif', 'jpg': '.jpg', 'png': '.png'}


class PlanError(Exception):
    pass


CPU_ENCODER = {'h264': 'libx264', 'hevc': 'libx265', 'av1': 'libsvtav1'}


def need_encoder(caps: dict, name: str, what: str):
    """Fail early, with a fix, when the ffmpeg in use lacks an encoder."""
    have = caps.get('encoders')
    if have and name not in have:
        raise PlanError(f'This ffmpeg has no {name} for {what}. Use the GPU encoder, or download '
                        f'Converter’s own ffmpeg from the setup prompt')


# ── helpers ────────────────────────────────────────────────────────────────

def effective_duration(info: dict, trim) -> float:
    dur = info.get('duration') or 0
    if trim:
        start, end = trim
        end = end if end is not None else dur
        return max(0.0, end - (start or 0))
    return dur


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def fit_short(w: int, h: int, short: int) -> tuple[int, int]:
    """Scale so the short side is `short`, never upscaling. Even dims."""
    if not short or min(w, h) <= short:
        return _even(w) if w % 2 else w, _even(h) if h % 2 else h
    if w >= h:
        return _even(w * short / h), short
    return short, _even(h * short / w)


def fit_long(w: int, h: int, long: int) -> tuple[int, int]:
    if not long or max(w, h) <= long:
        return w, h
    k = long / max(w, h)
    return max(1, round(w * k)), max(1, round(h * k))


def fmt_kbps(k: float) -> str:
    return f'{k / 1000:.1f} Mbps' if k >= 1000 else f'{int(k)} kbps'


def trim_args(trim, before_input: bool) -> list[str]:
    if not trim:
        return []
    start, end = trim
    if before_input:
        return ['-ss', f'{start:.3f}'] if start else []
    if end is not None:
        return ['-t', f'{end - (start or 0):.3f}']
    return []


# ── video ──────────────────────────────────────────────────────────────────

def video_plan(info: dict, s: dict, trim, caps: dict) -> dict:
    codec = s['codec']
    enc = 'gpu' if s['encoder'] == 'gpu' and caps.get('nvenc', {}).get(codec) else 'cpu'
    w, h = info.get('width') or 0, info.get('height') or 0
    src_fps = info.get('fps') or 30
    dur = effective_duration(info, trim)
    if not w or not h:
        raise PlanError('Could not read the video size')

    fps = src_fps
    if s['fps'] and s['fps'] < src_fps - 0.5:
        fps = s['fps']
    cap_short = s['resolution'] or min(w, h)

    # Audio
    has_audio = bool(info.get('audio_tracks'))
    amode, akbps = 'none', 0
    if has_audio and s['audio'] != 'none':
        if s['audio'] == 'auto':
            if s['mode'] == 'size':
                amode = 'aac'
            elif s['container'] == 'mkv' or all(c in MP4_AUDIO_OK for c in info.get('audio_codecs', [])):
                amode = 'copy'
            else:
                amode = 'aac'
        else:
            amode = s['audio']
        akbps = s['audio_kbps'] if amode != 'copy' else (info.get('abitrate') or 160_000) / 1000

    if enc == 'cpu':
        need_encoder(caps, CPU_ENCODER[codec], CODEC_LABEL[codec])
    plan = {'codec': codec, 'encoder': enc, 'fps': fps, 'amode': amode, 'akbps': akbps,
            'duration': dur, 'mode': s['mode'], 'container': s['container']}

    if s['mode'] == 'size':
        if dur <= 0:
            raise PlanError('Unknown duration, so the size can’t be targeted')
        target_bytes = s['target_mb'] * 1_000_000
        total_kbps = target_bytes * 8 / 1000 / dur * 0.965   # container overhead
        if amode != 'none':
            akbps = min(akbps, 128)
            if total_kbps < 700:
                akbps = 96
            if total_kbps < 300:
                akbps = 64
            plan['akbps'] = akbps
        vkbps = total_kbps - akbps * len(info.get('audio_codecs') or [1]) if amode != 'none' else total_kbps
        if vkbps < 60:
            need = math.ceil((60 + akbps) * dur / 8 / 1000 / 0.965)
            raise PlanError(f'Too long for {s["target_mb"]} MB. Trim it or allow at least {need} MB')
        plan['vkbps'] = int(vkbps)

        short = min(min(w, h), cap_short)
        if s['auto_scale']:
            ladder = [short] + [x for x in SHORT_LADDER if x < short]
            for cand in ladder:
                cw, ch = fit_short(w, h, cand)
                cfps = fps
                if vkbps * 1000 / (cw * ch * cfps) < MIN_BPP[codec] and cfps > 31:
                    cfps = 30
                if vkbps * 1000 / (cw * ch * cfps) >= MIN_BPP[codec] or cand == ladder[-1]:
                    short, fps = cand, cfps
                    break
            plan['fps'] = fps
        plan['width'], plan['height'] = fit_short(w, h, short)
        summary = f'{plan["height"] if w >= h else plan["width"]}p · {fmt_kbps(vkbps)}'
        plan['estimate'] = int(target_bytes * 0.97)
    else:
        plan['width'], plan['height'] = fit_short(w, h, cap_short)
        plan['crf'] = CRF[(codec, enc)][s['quality']]
        short_out = plan['height'] if w >= h else plan['width']
        summary = f'{short_out}p · {QUALITY_NAMES[s["quality"]]}'
        plan['estimate'] = None

    if fps != src_fps:
        summary += f' · {fps:g} fps'
    plan['scaled'] = (plan['width'], plan['height']) != (w, h)
    plan['ten_bit'] = codec != 'h264' and (info.get('bit_depth') or 8) >= 10
    plan['summary'] = f'{CODEC_LABEL[codec]} {"GPU" if enc == "gpu" else "CPU"} · {summary}'
    return plan


def _video_cmd(src: str, info: dict, s: dict, plan: dict, trim, out: str,
               pass_no: int | None = None, vkbps: int | None = None) -> list[str]:
    codec, enc = plan['codec'], plan['encoder']
    a = trim_args(trim, True) + ['-i', src] + trim_args(trim, False)
    a += ['-map', f'0:{info["video_index"]}']
    analysis = pass_no == 1
    if plan['amode'] != 'none' and not analysis:
        a += ['-map', '0:a?']
    if s['container'] == 'mkv' and info.get('has_subs') and not analysis:
        a += ['-map', '0:s?', '-c:s', 'copy']

    vf = []
    if plan['fps'] != (info.get('fps') or plan['fps']):
        vf.append(f'fps={plan["fps"]:g}')
    if plan['scaled']:
        vf.append(f'scale={plan["width"]}:{plan["height"]}:flags=lanczos')
    if vf:
        a += ['-vf', ','.join(vf)]

    if enc == 'gpu':
        a += ['-pix_fmt', 'p010le' if plan['ten_bit'] else 'yuv420p']
        a += ['-c:v', NVENC_NAME[codec], '-preset', SPEED['nvenc'][s['speed']], '-tune', 'hq',
              '-spatial-aq', '1', '-temporal-aq', '1', '-rc-lookahead', '32']
        if plan['mode'] == 'size':
            a += ['-rc', 'vbr', '-b:v', f'{vkbps}k', '-maxrate', f'{int(vkbps * 1.6)}k',
                  '-bufsize', f'{vkbps * 2}k', '-multipass', 'fullres']
        else:
            a += ['-rc', 'vbr', '-cq', str(plan['crf']), '-b:v', '0']
    else:
        a += ['-pix_fmt', 'yuv420p10le' if plan['ten_bit'] else 'yuv420p']
        if codec == 'h264':
            a += ['-c:v', 'libx264', '-preset', SPEED['x26x'][s['speed']]]
            if plan['mode'] == 'size':
                a += ['-b:v', f'{vkbps}k', '-pass', str(pass_no), '-passlogfile', 'ffpass']
            else:
                a += ['-crf', str(plan['crf'])]
        elif codec == 'hevc':
            a += ['-c:v', 'libx265', '-preset', SPEED['x26x'][s['speed']]]
            if plan['mode'] == 'size':
                # Relative stats path: x265-params uses ':' as a separator, which
                # breaks on Windows drive letters. The job runs with cwd=tmpdir.
                a += ['-b:v', f'{vkbps}k', '-x265-params', f'pass={pass_no}:stats=x265.log:log-level=error']
            else:
                a += ['-crf', str(plan['crf']), '-x265-params', 'log-level=error']
        else:
            a += ['-c:v', 'libsvtav1', '-preset', SPEED['svt'][s['speed']], '-svtav1-params', 'tune=0']
            if plan['mode'] == 'size':
                a += ['-b:v', f'{vkbps}k']
            else:
                a += ['-crf', str(plan['crf'])]

    if codec == 'hevc' and s['container'] == 'mp4':
        a += ['-tag:v', 'hvc1']   # needed for Apple / QuickTime playback

    if analysis:
        return a + ['-an', '-f', 'null', '-']

    if plan['amode'] == 'copy':
        a += ['-c:a', 'copy']
    elif plan['amode'] == 'aac':
        a += ['-c:a', 'aac', '-b:a', f'{int(plan["akbps"])}k']
    elif plan['amode'] == 'opus':
        a += ['-c:a', 'libopus', '-b:a', f'{int(plan["akbps"])}k']
    a += ['-map_metadata', '0', '-map_chapters', '0']
    if s['container'] == 'mp4':
        a += ['-movflags', '+faststart']
    return a + [out]


def video_execute(ctx):
    s, info, plan = ctx.settings, ctx.info, ctx.plan
    two_pass = plan['mode'] == 'size' and plan['encoder'] == 'cpu' and plan['codec'] in ('h264', 'hevc')
    vkbps = plan.get('vkbps')
    target = s['target_mb'] * 1_000_000 if plan['mode'] == 'size' else None
    attempts = 3 if target else 1

    for attempt in range(attempts):
        retry = f' (retry {attempt})' if attempt else ''
        if two_pass:
            ctx.stage('Analyzing' + retry)
            ctx.ffmpeg(_video_cmd(ctx.src, info, s, plan, ctx.trim, '', 1, vkbps), span=(0, 0.3))
            ctx.stage('Encoding' + retry)
            ctx.ffmpeg(_video_cmd(ctx.src, info, s, plan, ctx.trim, ctx.out, 2, vkbps), span=(0.3, 1))
        else:
            ctx.stage('Encoding' + retry)
            ctx.ffmpeg(_video_cmd(ctx.src, info, s, plan, ctx.trim, ctx.out, None, vkbps), span=(0, 1))
        if not target:
            return
        size = Path(ctx.out).stat().st_size
        if size <= target:
            return
        vkbps = int(vkbps * (target / size) * 0.95)
        ctx.note(f'First pass came out at {size / 1e6:.1f} MB, re-encoding at {fmt_kbps(vkbps)}')
    ctx.warn(f'Still over {s["target_mb"]} MB after {attempts} tries')


# ── gif ────────────────────────────────────────────────────────────────────

GIF_EXT = {'gif': '.gif', 'webp': '.webp', 'mp4': '.mp4'}


def gif_plan(info: dict, s: dict, trim, caps: dict) -> dict:
    w, h = info.get('width') or 0, info.get('height') or 0
    if not w:
        raise PlanError('Could not read the frame size')
    src_fps = info.get('fps') or 10
    fps = s['fps'] if s['fps'] and s['fps'] < src_fps - 0.5 else None
    nw, nh = fit_long(w, h, s['max_side'])
    fmt = s['format']
    if fmt == 'webp':
        need_encoder(caps, 'libwebp_anim', 'animated WebP')
    elif fmt == 'mp4':
        need_encoder(caps, 'libx264', 'MP4')
    parts = [f'{nw}×{nh}']
    if fps:
        parts.append(f'{fps} fps')
    if fmt == 'gif':
        parts.append(f'{s["colors"]} colors')
        if caps.get('gifsicle') and s['lossy']:
            parts.append(f'lossy {s["lossy"]}')
    else:
        parts.append(f'quality {s["quality"]}')
    if s['limit_on']:
        parts.append(f'≤ {s["limit_mb"]} MB')
    dur = effective_duration(info, trim)
    if info.get('kind') == 'video' and fmt == 'gif' and dur > 20:
        parts.append(f'{dur:.0f} s long, trim it for a smaller GIF')
    return {'width': nw, 'height': nh, 'fps': fps, 'format': fmt,
            'summary': f'{fmt.upper()} · ' + ' · '.join(parts), 'estimate': None}


def _gif_cmd(src, info, s, w, h, fps, colors, trim, out):
    a = trim_args(trim, True) + ['-i', src] + trim_args(trim, False)
    vin = f'0:{info.get("video_index", "v:0")}'
    pre = []
    if fps:
        pre.append(f'fps={fps}')
    if (w, h) != (info['width'], info['height']):
        pre.append(f'scale={w}:{h}:flags=lanczos')
    fmt = s['format']
    if fmt == 'gif':
        d = {'bayer': 'bayer:bayer_scale=3', 'none': 'none'}.get(s['dither'], s['dither'])
        chain = ','.join(pre) or 'null'
        fc = (f'[{vin}]{chain},split[a][b];[a]palettegen=max_colors={colors}:stats_mode=diff[p];'
              f'[b][p]paletteuse=dither={d}:diff_mode=rectangle')
        return a + ['-filter_complex', fc, '-loop', '0', out]
    if fmt == 'webp':
        a += ['-map', vin]
        if pre:
            a += ['-vf', ','.join(pre)]
        return a + ['-c:v', 'libwebp_anim', '-lossless', '0', '-quality', str(s['quality']),
                    '-compression_level', '5', '-loop', '0', out]
    # mp4
    a += ['-map', vin]
    pre.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')
    crf = round(35 - s['quality'] * 0.17)
    return a + ['-vf', ','.join(pre), '-c:v', 'libx264', '-preset', 'slow', '-crf', str(crf),
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an', out]


def gif_execute(ctx):
    s, info, plan = ctx.settings, ctx.info, ctx.plan
    w, h, fps, colors = plan['width'], plan['height'], plan['fps'], s['colors']
    use_gifsicle = s['format'] == 'gif' and TOOLS.gifsicle
    limit = s['limit_mb'] * 1_000_000 if s['limit_on'] else None
    attempts = 5 if limit else 1
    raw = str(ctx.tmp / ('raw' + GIF_EXT[s['format']]))

    for attempt in range(attempts):
        ctx.stage('Encoding' + (f' (smaller, try {attempt + 1})' if attempt else ''))
        target = raw if use_gifsicle else ctx.out
        ctx.ffmpeg(_gif_cmd(ctx.src, info, s, w, h, fps, colors, ctx.trim, target),
                   span=(0, 0.75 if use_gifsicle else 1))
        if use_gifsicle:
            ctx.stage('Optimizing')
            args = [TOOLS.gifsicle, '-O3', '--no-warnings']
            if s['lossy']:
                args.append(f'--lossy={s["lossy"]}')
            ctx.tool(args + ['-o', 'opt.gif', 'raw.gif'], span=(0.75, 1), cwd=str(ctx.tmp))
            shutil.move(str(ctx.tmp / 'opt.gif'), ctx.out)
        if not limit:
            return
        size = Path(ctx.out).stat().st_size
        if size <= limit:
            return
        k = math.sqrt(limit / size) * 0.92
        w, h = max(16, round(w * k)), max(16, round(h * k))
        if attempt >= 2 and colors > 64:
            colors //= 2
        ctx.note(f'{size / 1e6:.1f} MB was over the limit, shrinking to {w}×{h}')
    ctx.warn(f'Still over {s["limit_mb"]} MB. Try a lower frame rate or a shorter clip')


# ── audio ──────────────────────────────────────────────────────────────────

def audio_plan(info: dict, s: dict, trim, caps: dict) -> dict:
    fmt = s['format']
    dur = effective_duration(info, trim)
    kbps = {'opus': s['opus_kbps'], 'mp3': s['mp3_kbps'], 'aac': s['aac_kbps']}.get(fmt)
    if fmt == 'opus' and not caps.get('opusenc'):
        raise PlanError('Opus needs opusenc. Download it from the Audio settings')
    if fmt == 'mp3':
        need_encoder(caps, 'libmp3lame', 'MP3')
    est = int(dur * kbps * 1000 / 8 * 1.01) if kbps and dur else None
    label = {'opus': 'Opus', 'mp3': 'MP3', 'aac': 'AAC', 'flac': 'FLAC'}[fmt]
    summary = f'{label} {kbps} kbps' if kbps else f'{label} lossless'
    if info.get('has_cover') and s['cover'] != 'keep':
        summary += ' · ' + ('cover shrunk' if s['cover'] == 'shrink' else 'no cover')
    return {'format': fmt, 'kbps': kbps, 'summary': summary, 'estimate': est}


COVER_SHRINK_VF = "scale='min(800,iw)':'min(800,ih)':force_original_aspect_ratio=decrease"


def audio_execute(ctx):
    s, info, plan = ctx.settings, ctx.info, ctx.plan
    fmt, cover = s['format'], s['cover']
    has_cover = info.get('has_cover')
    src = ctx.src

    if fmt == 'opus':
        pic_args: list[str] = []
        if has_cover and cover == 'remove':
            pic_args = ['--discard-pictures']
        elif has_cover and cover == 'shrink':
            ctx.stage('Resizing cover')
            ctx.ffmpeg(['-i', src, '-map', f'0:{info["cover_index"]}', '-frames:v', '1',
                        '-vf', COVER_SHRINK_VF, '-q:v', '3', 'cover.jpg'],
                       span=(0, 0.05), cwd=str(ctx.tmp), progress=False)
            pic_args = ['--discard-pictures', '--picture', 'cover.jpg']

        direct = Path(src).suffix.lower() in DIRECT_OPUSENC and not ctx.trim and \
            (info.get('acodec') == 'flac' or str(info.get('acodec', '')).startswith('pcm_'))
        if not direct:
            # Anything opusenc can't read goes through a lossless FLAC first, which
            # carries the tags and cover art along.
            ctx.stage('Decoding')
            a = trim_args(ctx.trim, True) + ['-i', src] + trim_args(ctx.trim, False) + ['-map', '0:a:0']
            if has_cover and cover == 'keep':
                a += ['-map', f'0:{info["cover_index"]}', '-c:v', 'copy', '-disposition:v', 'attached_pic']
            a += ['-map_metadata', '0', '-c:a', 'flac', '-compression_level', '0',
                  '-sample_fmt', 's32', '-fflags', '+bitexact', 'in.flac']
            ctx.ffmpeg(a, span=(0.05, 0.4), cwd=str(ctx.tmp))
            src = 'in.flac'
        ctx.stage('Encoding')
        ctx.tool([TOOLS.opusenc, '--bitrate', str(s['opus_kbps']), *pic_args, src, ctx.out],
                 span=(0.4 if not direct else 0.05, 1), cwd=str(ctx.tmp),
                 duration=effective_duration(info, ctx.trim))
        return

    ctx.stage('Encoding')
    a = trim_args(ctx.trim, True) + ['-i', src] + trim_args(ctx.trim, False) + ['-map', '0:a:0']
    if has_cover and cover != 'remove' and fmt in ('mp3', 'aac', 'flac'):
        a += ['-map', f'0:{info["cover_index"]}', '-disposition:v:0', 'attached_pic']
        if cover == 'shrink':
            a += ['-c:v', 'mjpeg', '-q:v', '3', '-vf', COVER_SHRINK_VF]
        else:
            a += ['-c:v', 'copy']
    a += ['-map_metadata', '0']
    if fmt == 'mp3':
        a += ['-c:a', 'libmp3lame', '-b:a', f'{s["mp3_kbps"]}k', '-id3v2_version', '3']
    elif fmt == 'aac':
        a += ['-c:a', 'aac', '-b:a', f'{s["aac_kbps"]}k', '-movflags', '+faststart']
    else:
        a += ['-c:a', 'flac', '-compression_level', '8']
    ctx.ffmpeg(a + [ctx.out], span=(0, 1))


# ── image ──────────────────────────────────────────────────────────────────

def image_plan(info: dict, s: dict, trim, caps: dict) -> dict:
    w, h = info.get('width') or 0, info.get('height') or 0
    nw, nh = fit_long(w, h, s['max_side']) if w else (0, 0)
    fmt = s['format']
    q = '' if fmt == 'png' else f' · quality {s["quality"]}'
    return {'format': fmt, 'width': nw, 'height': nh, 'estimate': None,
            'summary': f'{fmt.upper()} · {nw}×{nh}{q}'}


def image_execute(ctx):
    from PIL import Image, ImageOps
    s = ctx.settings
    ctx.stage('Converting')
    with Image.open(ctx.src) as im:
        icc = im.info.get('icc_profile')
        im = ImageOps.exif_transpose(im)
        if s['max_side'] and max(im.size) > s['max_side']:
            im.thumbnail((s['max_side'], s['max_side']), Image.LANCZOS)
        fmt, q = s['format'], s['quality']
        kw = {'icc_profile': icc} if icc else {}
        if fmt == 'jpg':
            if im.mode in ('RGBA', 'LA', 'P', 'PA'):
                im = im.convert('RGBA')
                bg = Image.new('RGB', im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel('A'))
                im = bg
            elif im.mode != 'RGB':
                im = im.convert('RGB')
            im.save(ctx.out, 'JPEG', quality=q, optimize=True, progressive=True, **kw)
        elif fmt == 'png':
            im.save(ctx.out, 'PNG', optimize=True, **kw)
        elif fmt == 'webp':
            im.save(ctx.out, 'WEBP', quality=q, method=6, **kw)
        else:
            if im.mode not in ('RGB', 'RGBA'):
                im = im.convert('RGBA' if 'A' in im.mode else 'RGB')
            im.save(ctx.out, 'AVIF', quality=q, **kw)
    ctx.progress(1.0)


# ── registry ───────────────────────────────────────────────────────────────

PLANNERS = {'video': video_plan, 'gif': gif_plan, 'audio': audio_plan, 'image': image_plan}
EXECUTORS = {'video': video_execute, 'gif': gif_execute, 'audio': audio_execute, 'image': image_execute}

# Which tasks make sense for a given input kind (first = default).
TASKS_FOR_KIND = {
    'video': ['video', 'gif', 'audio'],
    'gif': ['gif'],
    'audio': ['audio'],
    'image': ['image'],
}


def output_ext(task: str, s: dict, info: dict, src: str) -> str:
    if task == 'video':
        return '.' + s['container']
    if task == 'gif':
        return GIF_EXT[s['format']]
    if task == 'audio':
        return AUDIO_EXT[s['format']]
    return IMAGE_EXT[s['format']]
