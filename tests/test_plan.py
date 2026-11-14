"""Pure planning tests: no ffmpeg needed.   .venv\\Scripts\\python -m unittest tests.test_plan"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converter import tasks  # noqa: E402
from converter.settings import DEFAULTS  # noqa: E402

CAPS = {'nvenc': {'h264': True, 'hevc': True, 'av1': True}, 'opusenc': True, 'gifsicle': False}


def vinfo(w=1920, h=1080, fps=60.0, dur=60.0, **kw):
    base = {'kind': 'video', 'width': w, 'height': h, 'fps': fps, 'duration': dur, 'size': 100_000_000,
            'video_index': 0, 'audio_tracks': 1, 'audio_codecs': ['aac'], 'abitrate': 160_000, 'bit_depth': 8}
    base.update(kw)
    return base


def vs(**kw):
    s = dict(DEFAULTS['video'])
    s.update(kw)
    return s


class FitTests(unittest.TestCase):
    def test_landscape_and_portrait_use_short_side(self):
        self.assertEqual(tasks.fit_short(3840, 2160, 1080), (1920, 1080))
        self.assertEqual(tasks.fit_short(1080, 1920, 720), (720, 1280))

    def test_never_upscales_and_keeps_even(self):
        self.assertEqual(tasks.fit_short(1280, 720, 1080), (1280, 720))
        w, h = tasks.fit_short(1281, 721, 0)
        self.assertEqual((w % 2, h % 2), (0, 0))


class VideoPlanTests(unittest.TestCase):
    def test_quality_mode_4k_to_1080_hevc(self):
        p = tasks.video_plan(vinfo(3840, 2160), vs(resolution=1080), None, CAPS)
        self.assertEqual((p['width'], p['height']), (1920, 1080))
        self.assertEqual(p['encoder'], 'gpu')
        self.assertEqual(p['amode'], 'copy')

    def test_gpu_falls_back_to_cpu(self):
        p = tasks.video_plan(vinfo(), vs(codec='av1'), None, {'nvenc': {}})
        self.assertEqual(p['encoder'], 'cpu')

    def test_size_mode_downscales_when_budget_is_tight(self):
        # 10 MB for 3 minutes of 1080p60 is ~430 kbps: should drop resolution and fps.
        p = tasks.video_plan(vinfo(dur=180), vs(mode='size', target_mb=10, codec='h264'), None, CAPS)
        self.assertLess(p['height'], 1080)
        self.assertEqual(p['fps'], 30)
        self.assertEqual(p['amode'], 'aac')
        total_kbps = p['vkbps'] + p['akbps']
        self.assertLessEqual(total_kbps * 180 / 8, 10_000)  # kB

    def test_size_mode_keeps_resolution_when_budget_allows(self):
        p = tasks.video_plan(vinfo(dur=20), vs(mode='size', target_mb=50), None, CAPS)
        self.assertEqual((p['width'], p['height']), (1920, 1080))

    def test_size_mode_rejects_impossible_target(self):
        with self.assertRaises(tasks.PlanError) as cm:
            tasks.video_plan(vinfo(dur=3600), vs(mode='size', target_mb=10), None, CAPS)
        self.assertIn('Too long', str(cm.exception))

    def test_trim_shortens_duration_for_size_math(self):
        full = tasks.video_plan(vinfo(dur=600), vs(mode='size', target_mb=25), None, CAPS)
        trimmed = tasks.video_plan(vinfo(dur=600), vs(mode='size', target_mb=25), (100, 130), CAPS)
        self.assertGreater(trimmed['vkbps'], full['vkbps'] * 10)

    def test_ten_bit_kept_for_hevc_not_h264(self):
        self.assertTrue(tasks.video_plan(vinfo(bit_depth=10), vs(codec='hevc'), None, CAPS)['ten_bit'])
        self.assertFalse(tasks.video_plan(vinfo(bit_depth=10), vs(codec='h264'), None, CAPS)['ten_bit'])

    def test_pcm_audio_reencoded_for_mp4(self):
        p = tasks.video_plan(vinfo(audio_codecs=['pcm_s16le']), vs(), None, CAPS)
        self.assertEqual(p['amode'], 'aac')
        p = tasks.video_plan(vinfo(audio_codecs=['pcm_s16le']), vs(container='mkv'), None, CAPS)
        self.assertEqual(p['amode'], 'copy')


class AudioPlanTests(unittest.TestCase):
    def test_estimate(self):
        info = {'kind': 'audio', 'duration': 240, 'size': 30_000_000}
        p = tasks.audio_plan(info, dict(DEFAULTS['audio']), None, CAPS)
        self.assertAlmostEqual(p['estimate'], 240 * 128_000 / 8, delta=50_000)

    def test_missing_opusenc_is_reported(self):
        with self.assertRaises(tasks.PlanError):
            tasks.audio_plan({'kind': 'audio', 'duration': 10}, dict(DEFAULTS['audio']), None, {'opusenc': False})


if __name__ == '__main__':
    unittest.main()
