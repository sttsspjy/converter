"""A tiny localhost file server with Range support, so <video> can preview
queued files for trimming without copying them anywhere."""
from __future__ import annotations

import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TYPES = {'.mkv': 'video/x-matroska', '.webm': 'video/webm', '.mp4': 'video/mp4', '.m4v': 'video/mp4',
         '.mov': 'video/mp4', '.gif': 'image/gif', '.flac': 'audio/flac', '.opus': 'audio/ogg',
         '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg', '.wav': 'audio/wav'}


class MediaServer:
    def __init__(self, resolve):
        """resolve(job_id) -> path or None"""
        self.token = secrets.token_urlsafe(16)
        self.resolve = resolve
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = self.path.split('?')[0].strip('/').split('/')
                if len(parts) != 3 or parts[0] != 'media' or parts[1] != server.token:
                    self.send_error(404)
                    return
                path = server.resolve(parts[2])
                if not path or not Path(path).is_file():
                    self.send_error(404)
                    return
                p = Path(path)
                size = p.stat().st_size
                ctype = TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
                start, end = 0, size - 1
                rng = self.headers.get('Range')
                status = 200
                if rng and rng.startswith('bytes='):
                    a, _, b = rng[6:].split(',')[0].partition('-')
                    try:
                        if a:
                            start = int(a)
                            end = int(b) if b else size - 1
                        else:
                            start = max(0, size - int(b))
                        end = min(end, size - 1)
                        status = 206
                    except ValueError:
                        pass
                    if start > end:
                        self.send_response(416)
                        self.send_header('Content-Range', f'bytes */{size}')
                        self.end_headers()
                        return
                length = end - start + 1
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(length))
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    with open(p, 'rb') as f:
                        f.seek(start)
                        left = length
                        while left > 0:
                            chunk = f.read(min(1 << 20, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except (ConnectionError, OSError):
                    pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name='media').start()

    def url(self, job_id: str) -> str:
        return f'http://127.0.0.1:{self.port}/media/{self.token}/{job_id}'
