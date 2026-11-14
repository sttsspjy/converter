"""The job queue: probing, scheduling, running, progress, cancellation."""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import tasks
from .probe import AUDIO_EXTS, IMAGE_EXTS, MEDIA_EXTS, ProbeError, probe, thumbnail
from .tools import IS_WIN, TOOLS, popen_flags

HEAVY = {'video', 'gif'}
LIGHT_SLOTS = max(2, min(8, (os.cpu_count() or 4) // 2))
ART_NAMES = {'cover', 'folder', 'front', 'album', 'albumart', 'albumartsmall', 'artwork'}
ART_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}

_TIME_RE = re.compile(rb'(\d+):(\d\d):(\d\d(?:\.\d+)?)')


class Cancelled(Exception):
    pass


class EncodeError(Exception):
    def __init__(self, msg: str, log: str = ''):
        super().__init__(msg)
        self.log = log


def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', str(p))]


class Job:
    PUBLIC = ('id', 'path', 'name', 'rel_dir', 'kind', 'task', 'tasks', 'info', 'thumb', 'trim',
              'status', 'progress', 'stage', 'eta', 'speed', 'error', 'log', 'notes', 'warning',
              'out', 'out_size', 'summary', 'started', 'finished', 'rev')

    def __init__(self, path: Path, rel_dir: str):
        self.id = uuid.uuid4().hex[:12]
        self.path = str(path)
        self.name = path.name
        self.rel_dir = rel_dir
        self.kind = None
        self.task = None
        self.tasks: list[str] = []
        self.info = None
        self.thumb = None
        self.trim = None            # (start, end|None) in seconds
        self.status = 'probing'     # probing ready queued running done error cancelled unsupported
        self.progress = 0.0
        self.stage = ''
        self.eta = None
        self.speed = None
        self.error = None
        self.log = ''
        self.notes: list[str] = []
        self.warning = None
        self.out = None
        self.out_size = None
        self.summary = None
        self.started = None
        self.finished = None
        self.rev = 0
        # private
        self.settings = None
        self.output = None
        self.general = None
        self.proc: subprocess.Popen | None = None
        self.cancel_flag = False

    def public(self) -> dict:
        return {k: getattr(self, k) for k in self.PUBLIC}


class JobContext:
    """What a task executor gets to work with."""

    def __init__(self, queue: 'Queue', job: Job, plan: dict, out: str, tmp: Path):
        self.q, self.job, self.plan = queue, job, plan
        self.src, self.out, self.tmp = job.path, out, tmp
        self.info, self.settings, self.trim = job.info, job.settings, job.trim
        self.duration = tasks.effective_duration(job.info, job.trim)

    # status
    def stage(self, text: str):
        self.q.update(self.job, stage=text)

    def progress(self, p: float):
        job = self.job
        p = max(job.progress, min(1.0, p))
        eta = None
        if job.started and p > 0.02:
            elapsed = time.time() - job.started
            eta = elapsed * (1 - p) / p
        self.q.update(job, progress=p, eta=eta)

    def note(self, text: str):
        """Record a retry; progress restarts for the new attempt."""
        self.q.update(self.job, notes=self.job.notes + [text], progress=0.0, eta=None)

    def warn(self, text: str):
        self.q.update(self.job, warning=text)

    # processes
    def ffmpeg(self, args, span=(0, 1), cwd=None, progress=True):
        full = [TOOLS.ffmpeg, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error']
        if progress:
            full += ['-progress', 'pipe:1', '-nostats']
        self._run(full + list(args), span, cwd, 'ffmpeg' if progress else None)

    def tool(self, args, span=(0, 1), cwd=None, duration=None):
        self._run(list(args), span, cwd, 'timecode' if duration else None, duration)

    def _run(self, args, span, cwd, parser, duration=None):
        job = self.job
        if job.cancel_flag:
            raise Cancelled()
        env = dict(os.environ, SVT_LOG='1')
        # Always run inside the job's temp dir: 2-pass encoders drop their stats
        # files in the working directory.
        proc = subprocess.Popen(
            args, cwd=cwd or str(self.tmp), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=popen_flags(job.general.get('low_priority', True)),
        )
        job.proc = proc
        err_tail: deque[str] = deque(maxlen=40)
        a, b = span
        dur = duration or self.duration

        def drain(stream, sink=None):
            for line in iter(stream.readline, b''):
                if sink is not None:
                    sink.append(line.decode('utf-8', 'replace').rstrip())

        if parser == 'ffmpeg':
            t = threading.Thread(target=drain, args=(proc.stderr, err_tail), daemon=True)
            t.start()
            for raw in iter(proc.stdout.readline, b''):
                key, _, val = raw.decode('ascii', 'ignore').strip().partition('=')
                if key == 'out_time_us' and dur > 0 and val.lstrip('-').isdigit():
                    self.progress(a + (b - a) * min(1.0, int(val) / 1e6 / dur))
                elif key == 'speed' and val.endswith('x'):
                    try:
                        job.speed = float(val[:-1])
                    except ValueError:
                        pass
            t.join(timeout=5)
        elif parser == 'timecode':
            # opusenc redraws one status line with \r and prints its position.
            t = threading.Thread(target=drain, args=(proc.stdout,), daemon=True)
            t.start()
            buf = b''
            while True:
                chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1') else proc.stderr.read(4096)
                if not chunk:
                    break
                buf = (buf + chunk)[-4096:]
                m = None
                for m in _TIME_RE.finditer(chunk):
                    pass
                if m and dur > 0:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    self.progress(a + (b - a) * min(1.0, secs / dur))
            for line in re.split(rb'[\r\n]+', buf):
                if line.strip():
                    err_tail.append(line.decode('utf-8', 'replace').strip())
            t.join(timeout=5)
        else:
            out_t = threading.Thread(target=drain, args=(proc.stdout,), daemon=True)
            out_t.start()
            drain(proc.stderr, err_tail)
            out_t.join(timeout=5)

        rc = proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        job.proc = None
        if job.cancel_flag:
            raise Cancelled()
        if rc != 0:
            lines = [l for l in err_tail if l and not l.startswith('Svt[info]')]
            log = '\n'.join(lines)
            msg = _pick_error(lines) or f'{Path(args[0]).stem} exited with code {rc}'
            raise EncodeError(msg, log)
        self.progress(b)


def _pick_error(lines: list[str]) -> str | None:
    keys = ('error', 'invalid', 'failed', 'unable', 'not supported', 'no such', 'cannot', 'unsupported')
    hits = [l for l in lines if any(k in l.lower() for k in keys)]
    pick = hits[-1] if hits else (lines[-1] if lines else None)
    if pick:
        pick = re.sub(r'^\[[^\]]+ @ [0-9a-fx]+\]\s*', '', pick)
    return pick


class Queue:
    def __init__(self, settings, on_change=None, on_idle=None):
        self.settings = settings
        self.on_change = on_change or (lambda: None)
        self.on_idle = on_idle or (lambda: None)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.rev = 0
        self.reserved: set[str] = set()
        self.prober = ThreadPoolExecutor(max_workers=4, thread_name_prefix='probe')
        self.running: dict[str, threading.Thread] = {}
        self.had_work = False
        threading.Thread(target=self._dispatch, daemon=True, name='dispatch').start()

    # ── state ──
    def update(self, job: Job, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(job, k, v)
            self.rev += 1
            job.rev = self.rev

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                'rev': self.rev,
                'ids': list(self.jobs),
                'jobs': [j.public() for j in self.jobs.values() if j.rev > since],
            }

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    # ── adding ──
    def add_paths(self, paths: list[str]) -> dict:
        found: list[tuple[Path, str]] = []
        skipped = 0
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                files = sorted((f for f in p.rglob('*') if f.is_file()), key=_natural_key)
                # Images that sit next to audio tracks are album art, not work.
                audio_dirs = {f.parent for f in files if f.suffix.lower() in AUDIO_EXTS}
                for f in files:
                    ext = f.suffix.lower()
                    if ext in IMAGE_EXTS and f.parent in audio_dirs:
                        skipped += 1
                    elif ext in MEDIA_EXTS and not f.name.startswith('.') and '.part.' not in f.name:
                        found.append((f, str(f.parent.relative_to(p.parent))))
                    else:
                        skipped += 1
            elif p.is_file():
                found.append((p, ''))
        added = 0
        with self.lock:
            existing = {j.path.lower() for j in self.jobs.values() if j.status not in ('done',)}
            new_jobs = []
            for f, rel in found:
                if str(f).lower() in existing:
                    skipped += 1
                    continue
                job = Job(f, rel)
                self.jobs[job.id] = job
                self.update(job)
                new_jobs.append(job)
                existing.add(str(f).lower())
                added += 1
        for job in new_jobs:
            self.prober.submit(self._probe, job)
        self.on_change()
        return {'added': added, 'skipped': skipped}

    def _probe(self, job: Job):
        try:
            info = probe(job.path)
            kind = info['kind']
            self.update(job, info=info, kind=kind, tasks=tasks.TASKS_FOR_KIND[kind],
                        task=tasks.TASKS_FOR_KIND[kind][0], status='ready')
            thumb = thumbnail(job.path, info)
            if thumb:
                self.update(job, thumb=thumb)
        except ProbeError as e:
            self.update(job, status='unsupported', error=str(e))
        except Exception as e:  # noqa: BLE001
            self.update(job, status='unsupported', error=f'Could not read file: {e}')
        self.on_change()

    # ── editing ──
    def set_task(self, job_id: str, task: str):
        job = self.jobs.get(job_id)
        if job and task in job.tasks and job.status in ('ready', 'done', 'error', 'cancelled'):
            self.update(job, task=task, status='ready', progress=0.0, error=None, warning=None, notes=[])

    def set_trim(self, job_id: str, start, end):
        job = self.jobs.get(job_id)
        if not job or job.status in ('queued', 'running'):
            return
        dur = (job.info or {}).get('duration') or 0
        start = max(0.0, float(start or 0))
        end = float(end) if end not in (None, '') else None
        if end is not None and (end >= dur - 0.05):
            end = None
        trim = None if (start < 0.05 and end is None) else (round(start, 3), None if end is None else round(end, 3))
        self.update(job, trim=trim)

    def remove(self, ids: list[str]):
        self.cancel(ids)
        with self.lock:
            for i in ids:
                job = self.jobs.get(i)
                if job and job.status != 'running':
                    del self.jobs[i]
            self.rev += 1

    def clear(self, which: str):
        with self.lock:
            for i, job in list(self.jobs.items()):
                if which == 'finished' and job.status == 'done':
                    del self.jobs[i]
                elif which == 'all' and job.status not in ('running', 'queued'):
                    del self.jobs[i]
            self.rev += 1

    # ── running ──
    def start(self, ids: list[str] | None = None) -> int:
        s = self.settings.get()
        n = 0
        with self.lock:
            for job in self.jobs.values():
                startable = job.status == 'ready' if ids is None else \
                    (job.id in ids and job.status in ('ready', 'error', 'cancelled', 'done'))
                if not startable or not job.task:
                    continue
                job.settings = s[job.task]
                job.output = s['output']
                job.general = s['general']
                job.cancel_flag = False
                self.update(job, status='queued', progress=0.0, stage='Waiting', eta=None,
                            error=None, log='', warning=None, notes=[], out=None, out_size=None)
                n += 1
            self.cv.notify_all()
        return n

    def cancel(self, ids: list[str] | None = None):
        with self.lock:
            for job in self.jobs.values():
                if ids is not None and job.id not in ids:
                    continue
                if job.status == 'queued':
                    self.update(job, status='ready', stage='', progress=0.0)
                elif job.status == 'running':
                    job.cancel_flag = True
                    proc = job.proc
                    if proc and proc.poll() is None:
                        try:
                            proc.kill()
                        except OSError:
                            pass

    def busy(self) -> bool:
        with self.lock:
            return any(j.status in ('queued', 'running') for j in self.jobs.values())

    def overall(self) -> tuple[int, int, float]:
        """(active jobs, active jobs, mean progress of active jobs)"""
        with self.lock:
            active = [j for j in self.jobs.values() if j.status in ('queued', 'running')]
            if not active:
                return 0, 0, 0.0
            total = len(active)
            frac = sum(j.progress for j in active) / total
            return len(active), total, frac

    def _dispatch(self):
        keep_awake = False
        while True:
            with self.cv:
                self.cv.wait(timeout=1.0)
                heavy_running = sum(1 for j in self.jobs.values() if j.status == 'running' and j.task in HEAVY)
                light_running = sum(1 for j in self.jobs.values() if j.status == 'running' and j.task not in HEAVY)
                for job in self.jobs.values():
                    if job.status != 'queued':
                        continue
                    if job.task in HEAVY:
                        if heavy_running >= 1:
                            continue
                        heavy_running += 1
                    else:
                        if light_running >= LIGHT_SLOTS:
                            continue
                        light_running += 1
                    self.update(job, status='running', started=time.time(), stage='Starting')
                    threading.Thread(target=self._run, args=(job,), daemon=True).start()
                active = any(j.status in ('queued', 'running') for j in self.jobs.values())
            if active:
                self.had_work = True
            want_awake = active and self.settings.section('general').get('keep_awake', True)
            if IS_WIN and want_awake != keep_awake:
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want_awake else 0))
                keep_awake = want_awake
            if not active and self.had_work:
                self.had_work = False
                try:
                    self.on_idle()
                except Exception:
                    pass

    def _resolve_output(self, job: Job, ext: str) -> Path | None:
        src = Path(job.path)
        o = job.output
        if o['mode'] == 'folder' and o['folder']:
            base = Path(o['folder']) / job.rel_dir
        else:
            base = src.parent
        stem = src.stem
        cand = base / (stem + ext)
        same_as_src = cand.resolve() == src.resolve()
        conflict = o.get('conflict', 'rename')
        if not same_as_src and cand.exists() and str(cand).lower() not in self.reserved:
            if conflict == 'skip':
                return None
            if conflict == 'overwrite':
                return cand
        n = 1
        while same_as_src or cand.exists() or str(cand).lower() in self.reserved:
            same_as_src = False
            cand = base / (f'{stem} (compressed){ext}' if n == 1 else f'{stem} (compressed {n}){ext}')
            n += 1
        return cand

    def _run(self, job: Job):
        tmp = Path(tempfile.mkdtemp(prefix='converter-'))
        part: Path | None = None
        final: Path | None = None
        try:
            caps = TOOLS.caps()
            plan = tasks.PLANNERS[job.task](job.info, job.settings, job.trim, caps)
            self.update(job, summary=plan['summary'])
            ext = tasks.output_ext(job.task, job.settings, job.info, job.path)
            with self.lock:
                final = self._resolve_output(job, ext)
                if final is None:
                    self.update(job, status='done', stage='Skipped (already exists)', progress=1.0,
                                finished=time.time(), out=None)
                    return
                self.reserved.add(str(final).lower())
            final.parent.mkdir(parents=True, exist_ok=True)
            part = final.with_name(f'{final.stem}.part{final.suffix}')
            ctx = JobContext(self, job, plan, str(part), tmp)
            tasks.EXECUTORS[job.task](ctx)
            os.replace(part, final)
            part = None
            if job.task == 'audio' and job.rel_dir and job.output['mode'] == 'folder' and job.output.get('copy_art'):
                _copy_album_art(Path(job.path).parent, final.parent)
            self.update(job, status='done', progress=1.0, stage='Done', eta=None,
                        out=str(final), out_size=final.stat().st_size, finished=time.time())
        except Cancelled:
            self.update(job, status='cancelled', stage='Cancelled', eta=None, finished=time.time())
        except tasks.PlanError as e:
            self.update(job, status='error', error=str(e), eta=None, finished=time.time())
        except EncodeError as e:
            self.update(job, status='error', error=str(e), log=e.log, eta=None, finished=time.time())
        except Exception as e:  # noqa: BLE001
            self.update(job, status='error', error=f'{type(e).__name__}: {e}', eta=None, finished=time.time())
        finally:
            job.proc = None
            if part is not None:
                for _ in range(10):   # the killed process may hold the file a moment
                    try:
                        part.unlink(missing_ok=True)
                        break
                    except PermissionError:
                        time.sleep(0.2)
            shutil.rmtree(tmp, ignore_errors=True)
            with self.cv:
                if final is not None:
                    self.reserved.discard(str(final).lower())
                self.cv.notify_all()
            self.on_change()

    def kill_all(self):
        with self.lock:
            for job in self.jobs.values():
                if job.status == 'queued':
                    job.status = 'ready'
            self.cancel([j.id for j in self.jobs.values() if j.status == 'running'])


def _copy_album_art(src_dir: Path, dst_dir: Path):
    if src_dir.resolve() == dst_dir.resolve():
        return
    try:
        for f in src_dir.iterdir():
            if f.is_file() and f.suffix.lower() in ART_EXTS and f.stem.lower() in ART_NAMES:
                dst = dst_dir / f.name
                if not dst.exists():
                    shutil.copy2(f, dst)
    except OSError:
        pass
