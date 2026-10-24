"""Read-only, loopback-only monitor; no execution, upload or arbitrary file routes."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
from urllib.parse import urlsplit

from .history import History
from .discussions import Discussions
from .council import public


class Monitor:
    def __init__(self, state_dir, port=0):
        self.history = History(state_dir)
        self.discussions = Discussions(state_dir)
        self.requested_port = port
        self.server = None
        self.thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/" if self.server else None

    def start(self):
        if self.server:
            return self.url
        monitor = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                port = self.server.server_port
                if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                    self.send_error(403)
                    return
                path = urlsplit(self.path).path
                try:
                    if path == "/":
                        body = files("agy_observer").joinpath("static/index.html").read_bytes()
                        kind = "text/html; charset=utf-8"
                    elif path == "/api/discussions":
                        body = json.dumps(
                            monitor.discussions.list(100), ensure_ascii=False
                        ).encode()
                        kind = "application/json; charset=utf-8"
                    elif re.fullmatch(r"/api/discussions/[A-Za-z0-9_-]{1,100}", path):
                        body = json.dumps(
                            public(monitor.discussions.read(path.rsplit("/", 1)[-1])),
                            ensure_ascii=False,
                        ).encode()
                        kind = "application/json; charset=utf-8"
                    elif path == "/api/runs":
                        body = json.dumps(
                            monitor.history.list(200, True), ensure_ascii=False
                        ).encode()
                        kind = "application/json; charset=utf-8"
                    elif re.fullmatch(r"/api/status/[A-Za-z0-9_-]{1,100}", path):
                        body = json.dumps(
                            monitor.history.status(path.rsplit("/", 1)[-1]), ensure_ascii=False
                        ).encode()
                        kind = "application/json; charset=utf-8"
                    elif re.fullmatch(r"/api/events/[A-Za-z0-9_-]{1,100}", path):
                        body = json.dumps(
                            monitor.history.events(path.rsplit("/", 1)[-1]), ensure_ascii=False
                        ).encode()
                        kind = "application/json; charset=utf-8"
                    else:
                        self.send_error(404)
                        return
                except (OSError, ValueError):
                    self.send_error(404)
                    return
                self.send_response(200)
                for key, value in {
                    "Content-Type": kind,
                    "Cache-Control": "no-store",
                    "X-AGY-Observer": "readonly-v1",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                }.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.requested_port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    async def open(self, include_tests=False, view=None):
        url = self.start()
        query = (["tests=1"] if include_tests else []) + (
            ["view=discussions"] if view == "discussions" else []
        )
        if query:
            url += "?" + "&".join(query)
        if os.name == "nt":
            os.startfile(url)
            return url
        # WSL: the service lives on Linux loopback; Windows browser access needs localhost forwarding.
        powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        if powershell.is_file():
            command = [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                f"Start-Process '{url}'",
            ]
        elif sys.platform == "darwin":
            command = ["/usr/bin/open", url]
        elif shutil.which("xdg-open"):
            command = [shutil.which("xdg-open"), url]
        else:
            command = None
        if command:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                code = await asyncio.wait_for(process.wait(), 5)
            except BaseException:
                process.kill()
                await asyncio.wait_for(process.wait(), 3)
                raise
            if code == 0:
                return url
        raise RuntimeError(
            "Could not request a visible browser. Open the monitor URL or explicitly configure headless operation."
        )

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None
