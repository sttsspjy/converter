"""Tool downloader against a local HTTP server and fake archives (nothing is executed)."""
import hashlib
import io
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converter import fetch  # noqa: E402


def make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='conv-fetch-'))
        self.www = self.root / 'www'
        self.www.mkdir()
        self.tools = self.root / 'tools'
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(self.www)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        patches = [mock.patch.object(fetch, 'TOOLS_DIR', self.tools),
                   mock.patch.object(fetch.TOOLS, 'refresh', lambda *a, **k: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.httpd.shutdown()
        shutil.rmtree(self.root, ignore_errors=True)

    def source(self, data: bytes, sha=None):
        (self.www / 'tool.zip').write_bytes(data)
        return {'label': 'Tool', 'url': f'{self.base}/tool.zip', 'sha256': sha or hashlib.sha256(data).hexdigest(),
                'size': len(data), 'keep': lambda n: n == 'tool.exe' or n.endswith('.dll'),
                'license': 'LICENSE', 'gives': ('tool',)}

    def run_fetch(self, src):
        with mock.patch.dict(fetch.SOURCES, {'tool': src}):
            f = fetch.Fetcher()
            self.assertTrue(f.start(['tool']))
            deadline = time.time() + 10
            while f.snapshot()['active'] and time.time() < deadline:
                time.sleep(0.05)
            return f.snapshot()

    def test_good_archive_extracts_only_needed_files(self):
        data = make_zip({'pkg/bin/tool.exe': b'exe', 'pkg/bin/lib.dll': b'dll', 'pkg/bin/other.exe': b'no',
                         'pkg/LICENSE': b'license text'})
        state = self.run_fetch(self.source(data))
        self.assertIsNone(state['error'])
        self.assertEqual(sorted(p.name for p in self.tools.iterdir()), ['lib.dll', 'tool-LICENSE', 'tool.exe'])

    def test_checksum_mismatch_is_rejected(self):
        data = make_zip({'tool.exe': b'exe'})
        state = self.run_fetch(self.source(data, sha='0' * 64))
        self.assertIn('checksum', state['error'])
        self.assertFalse((self.tools / 'tool.exe').exists())
        self.assertEqual([p for p in self.tools.iterdir()] if self.tools.exists() else [], [])

    def test_archive_without_the_tool_fails(self):
        state = self.run_fetch(self.source(make_zip({'readme.txt': b'hi'})))
        self.assertIn('didn’t contain', state['error'])

    def test_pinned_sources_are_https_and_hashed(self):
        for name, src in fetch.SOURCES.items():
            self.assertTrue(src['url'].startswith('https://'), name)
            self.assertRegex(src['sha256'], r'^[0-9a-f]{64}$', name)


if __name__ == '__main__':
    unittest.main()
