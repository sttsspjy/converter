"""End-to-end checks that drive the real queue against generated media.

    .venv\\Scripts\\python -m unittest tests.test_pipeline -v

Needs ffmpeg/ffprobe (and opusenc for the Opus tests) on PATH or in ./bin.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converter import settings as settings_mod  # noqa: E402
from converter.jobs import Queue  # noqa: E402
from converter.tools import TOOLS  # noqa: E402


def ff(*args):
    subprocess.run([TOOLS.ffmpeg, '-hide_banner', '-v', 'error', '-y', *args], check=True)


def probe_json(path):
    r = subprocess.run([TOOLS.ffprobe, '-v', 'error', '-print_format', 'json', '-show_format',
                        '-show_streams', str(path)], capture_output=True, check=True)
    return json.loads(r.stdout)


class FakeSettings(settings_mod.Settings):
    def __init__(self, tmp):
        self.path = Path(tmp) / 'settings.json'
        import threading
        self._lock = threading.Lock()
        self.data = settings_mod._merge(settings_mod.DEFAULTS, {})


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TOOLS.detect_hw()
        cls.dir = Path(tempfile.mkdtemp(prefix='conv-test-'))
        d = cls.dir
        ff('-f', 'lavfi', '-i', 'testsrc2=s=1280x720:r=30:d=8', '-f', 'lavfi', '-i', 'sine=f=440:d=8',
           '-c:v', 'libx264', '-crf', '12', '-c:a', 'aac', '-shortest', str(d / 'clip.mp4'))
        ff('-f', 'lavfi', '-i', 'testsrc2=s=400x300:r=20:d=3', str(d / 'anim.gif'))
        ff('-f', 'lavfi', '-i', 'testsrc2=s=500x500:d=1', '-frames:v', '1', str(d / 'cover.png'))
        album = d / 'Album 앨범'
        album.mkdir()
        for i in (1, 2):
            ff('-f', 'lavfi', '-i', f'sine=f={300 + i * 50}:d=12', '-i', str(d / 'cover.png'),
               '-map', '0', '-map', '1', '-c:a', 'flac', '-c:v', 'copy', '-disposition:v', 'attached_pic',
               '-metadata', f'title=곡 {i}', '-metadata', 'artist=가수', '-metadata', f'track={i}',
               str(album / f'0{i} 노래.flac'))
        shutil.copy(d / 'cover.png', album / 'cover.png')
        ff('-f', 'lavfi', '-i', 'sine=f=500:d=10', '-metadata', 'title=MP3 Song', '-b:a', '192k', str(d / 'song.mp3'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        self.settings = FakeSettings(self.dir)
        self.q = Queue(self.settings)

    def run_jobs(self, paths, patch=None, timeout=180):
        if patch:
            self.settings.data = settings_mod._merge(self.settings.data, patch)
        self.q.add_paths([str(p) for p in paths])
        deadline = time.time() + 30
        while any(j.status == 'probing' for j in self.q.jobs.values()):
            self.assertLess(time.time(), deadline, 'probe timed out')
            time.sleep(0.05)
        self.q.start()
        deadline = time.time() + timeout
        while self.q.busy():
            self.assertLess(time.time(), deadline, 'encode timed out')
            time.sleep(0.1)
        jobs = list(self.q.jobs.values())
        for j in jobs:
            self.assertEqual(j.status, 'done', f'{j.name}: {j.error}\n{j.log}')
        return jobs

    def test_video_quality_hevc(self):
        out_dir = self.dir / 'out_q'
        (job,) = self.run_jobs([self.dir / 'clip.mp4'], {
            'video': {'codec': 'hevc', 'encoder': 'cpu', 'speed': 'fast', 'resolution': 480},
            'output': {'mode': 'folder', 'folder': str(out_dir)}})
        info = probe_json(job.out)
        v = next(s for s in info['streams'] if s['codec_type'] == 'video')
        self.assertEqual(v['codec_name'], 'hevc')
        self.assertEqual(v['height'], 480)
        self.assertEqual(v.get('codec_tag_string'), 'hvc1')
        self.assertTrue(any(s['codec_type'] == 'audio' for s in info['streams']))
        self.assertFalse(list(out_dir.glob('*.part.*')))

    def test_video_size_target_two_pass(self):
        (job,) = self.run_jobs([self.dir / 'clip.mp4'], {
            'video': {'codec': 'h264', 'encoder': 'cpu', 'mode': 'size', 'target_mb': 1, 'speed': 'fast'},
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_s')}})
        self.assertLessEqual(job.out_size, 1_000_000)
        stray = [p for p in Path.cwd().glob('*') if p.name.startswith(('ffpass', 'x265'))]
        self.assertEqual(stray, [], 'pass logs must stay in the job temp dir')
        self.assertGreater(job.out_size, 600_000, 'should use most of the budget')

    def test_video_size_target_hevc_two_pass(self):
        (job,) = self.run_jobs([self.dir / 'clip.mp4'], {
            'video': {'codec': 'hevc', 'encoder': 'cpu', 'mode': 'size', 'target_mb': 0.6, 'speed': 'fast'},
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_s265')}})
        self.assertLessEqual(job.out_size, 600_000)
        self.assertGreater(job.out_size, 350_000)

    def test_video_beside_source_gets_suffix(self):
        (job,) = self.run_jobs([self.dir / 'clip.mp4'], {
            'video': {'codec': 'av1', 'encoder': 'gpu', 'quality': 4, 'resolution': 360},
            'output': {'mode': 'beside'}})
        self.assertEqual(Path(job.out).name, 'clip (compressed).mp4')
        Path(job.out).unlink()

    def test_trim(self):
        self.q.add_paths([str(self.dir / 'clip.mp4')])
        time.sleep(0.1)
        while any(j.status == 'probing' for j in self.q.jobs.values()):
            time.sleep(0.05)
        jid = next(iter(self.q.jobs))
        self.q.set_trim(jid, 2, 5)
        (job,) = self.run_jobs([], {'video': {'codec': 'h264', 'encoder': 'cpu', 'speed': 'fast'},
                                    'output': {'mode': 'folder', 'folder': str(self.dir / 'out_t')}})
        dur = float(probe_json(job.out)['format']['duration'])
        self.assertAlmostEqual(dur, 3.0, delta=0.25)

    def test_gif_compress(self):
        (job,) = self.run_jobs([self.dir / 'anim.gif'], {
            'gif': {'max_side': 200, 'fps': 10, 'colors': 64},
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_g')}})
        v = probe_json(job.out)['streams'][0]
        self.assertEqual(v['codec_name'], 'gif')
        self.assertEqual(max(v['width'], v['height']), 200)
        self.assertLess(job.out_size, (self.dir / 'anim.gif').stat().st_size)

    def test_gif_to_webp_and_limit(self):
        (job,) = self.run_jobs([self.dir / 'anim.gif'], {
            'gif': {'format': 'webp', 'limit_on': True, 'limit_mb': 0.05},
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_w')}})
        self.assertTrue(job.out.endswith('.webp'))
        self.assertLessEqual(job.out_size, 50_000)

    def test_album_to_opus_keeps_tags_and_cover(self):
        out_dir = self.dir / 'out_opus'
        jobs = self.run_jobs([self.dir / 'Album 앨범'], {
            'audio': {'format': 'opus', 'opus_kbps': 96},
            'output': {'mode': 'folder', 'folder': str(out_dir)}})
        self.assertEqual(len(jobs), 2)
        outs = sorted((out_dir / 'Album 앨범').glob('*.opus'))
        self.assertEqual([o.name for o in outs], ['01 노래.opus', '02 노래.opus'])
        self.assertTrue((out_dir / 'Album 앨범' / 'cover.png').exists(), 'album art copied')
        info = probe_json(outs[0])
        tags = {k.lower(): v for k, v in info['streams'][0].get('tags', {}).items()}
        self.assertEqual(tags.get('title'), '곡 1')
        self.assertEqual(tags.get('artist'), '가수')
        self.assertTrue(any(s['codec_type'] == 'video' for s in info['streams']), 'cover kept')

    def test_mp3_to_opus_via_flac_and_shrunk_cover(self):
        (job,) = self.run_jobs([self.dir / 'Album 앨범' / '01 노래.flac'], {
            'audio': {'format': 'opus', 'cover': 'shrink'},
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_shrink')}})
        info = probe_json(job.out)
        pic = next(s for s in info['streams'] if s['codec_type'] == 'video')
        self.assertLessEqual(pic['width'], 800)
        self.q = Queue(self.settings)
        (job,) = self.run_jobs([self.dir / 'song.mp3'], {
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_mp3')}})
        tags = {k.lower(): v for k, v in probe_json(job.out)['streams'][0].get('tags', {}).items()}
        self.assertEqual(tags.get('title'), 'MP3 Song')

    def test_skip_existing(self):
        out_dir = self.dir / 'out_skip'
        self.run_jobs([self.dir / 'song.mp3'], {'output': {'mode': 'folder', 'folder': str(out_dir), 'conflict': 'skip'}})
        self.q = Queue(self.settings)
        (job,) = self.run_jobs([self.dir / 'song.mp3'])
        self.assertIsNone(job.out)
        self.assertEqual(len(list(out_dir.iterdir())), 1, list(out_dir.iterdir()))

    def test_cancel_cleans_up(self):
        out_dir = self.dir / 'out_cancel'
        self.settings.data = settings_mod._merge(self.settings.data, {
            'video': {'codec': 'av1', 'encoder': 'cpu', 'speed': 'small'},
            'output': {'mode': 'folder', 'folder': str(out_dir)}})
        self.q.add_paths([str(self.dir / 'clip.mp4')])
        while any(j.status == 'probing' for j in self.q.jobs.values()):
            time.sleep(0.05)
        self.q.start()
        while not any(j.status == 'running' and j.proc for j in self.q.jobs.values()):
            time.sleep(0.05)
        time.sleep(0.5)
        self.q.cancel()
        while self.q.busy():
            time.sleep(0.1)
        job = next(iter(self.q.jobs.values()))
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(list(out_dir.glob('*')), [])

    def test_image(self):
        (job,) = self.run_jobs([self.dir / 'cover.png'], {
            'image': {'format': 'jpg', 'max_side': 200},
            'output': {'mode': 'folder', 'folder': str(self.dir / 'out_i')}})
        self.assertTrue(job.out.endswith('.jpg'))


if __name__ == '__main__':
    unittest.main()
