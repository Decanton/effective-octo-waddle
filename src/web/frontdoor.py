"""The review page's front door at http://127.0.0.1:8000. It stays up while the agent updates.

The real review app runs as a child process on a private port (8101 or 8102). This tiny
forwarder never changes. When an update is installed it starts the new app on the other
port, waits until it answers, switches traffic over, and only then stops the old one
(after any request it is still serving, such as a running submit, has finished).
So the page never goes offline and nothing in progress is cut off.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("jobsprint")
ROOT = Path(__file__).resolve().parents[2]
PORTS = None  # private ports are picked fresh each time, so nothing else can be in the way


def free_port() -> int:
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection", "content-length"}


class Backend:
    def __init__(self, port: int, cmd: list[str] | None = None):
        self.port, self.active, self.lock = port, 0, threading.Lock()
        # A secret only this child knows: proves the app answering on the port is the one we started.
        self.token = secrets.token_hex(12) if cmd is None else None
        self.cmd = cmd or [sys.executable, str(ROOT / "run_sprint.py"), "--review-backend", "--port", str(port)]
        env = {**os.environ, "JOBSPRINT_BACKEND_TOKEN": self.token or ""}
        self.proc = subprocess.Popen(self.cmd, cwd=str(ROOT), env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def healthy(self) -> bool:
        if self.proc.poll() is not None:
            return False
        try:
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            c.request("GET", "/api/version")
            r = c.getresponse()
            if r.status != 200:
                return False
            return self.token is None or json.loads(r.read() or b"{}").get("token") == self.token
        except Exception:
            return False

    def wait_ready(self, seconds: float = 60) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                return False
            if self.healthy():
                return True
            time.sleep(0.3)
        return False

    def stop(self, grace: float = 900) -> None:
        end = time.monotonic() + grace
        while self.active and time.monotonic() < end:
            time.sleep(1)
        try:
            self.proc.terminate()
            self.proc.wait(10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class FrontDoor:
    def __init__(self, ws, port: int = 8000, backend_factory=Backend, ports: tuple[int, int] | None = PORTS,
                 probation: float = 20.0):
        self.ws, self.port, self.factory, self.ports, self.probation = ws, port, backend_factory, ports, probation
        self.current: Backend | None = None
        self.switch_lock = threading.Lock()
        self.last_switch: dict = {}

    def start_backend(self) -> Backend:
        used = self.current.port if self.current else None
        port = next(p for p in self.ports if p != used) if self.ports else free_port()
        b = self.factory(port)
        if not b.wait_ready():
            b.stop(grace=0)
            raise RuntimeError("the new review app did not start")
        return b

    def swap(self) -> bool:
        """Start the new code next to the old, switch over, then retire the old one."""
        with self.switch_lock:
            try:
                new = self.start_backend()
            except Exception as exc:
                self.last_switch = {"ok": False, "error": str(exc), "at": time.time()}
                log.warning("Kept the running review page: %s", exc)
                return False
            old, self.current = self.current, new
            self.last_switch = {"ok": True, "at": time.time(), "port": new.port}
            threading.Thread(target=self._probation, args=(old, new), daemon=True).start()
            return True

    def _probation(self, old, new) -> None:
        """Keep the old version warm for a while; if the new one stops answering, switch back."""
        end = time.monotonic() + self.probation
        while time.monotonic() < end:
            if not new.healthy():
                time.sleep(1)
                if new.healthy():
                    continue
                if old and old.healthy():
                    self.current = old
                    self.last_switch = {"ok": False, "rolled_back": True, "at": time.time(),
                                        "error": "the new version stopped answering, so the previous one is back"}
                    log.warning("New review app failed after the switch; rolled back.")
                    new.stop(grace=0)
                    return
                break
            time.sleep(1)
        if old:
            old.stop()

    def update_loop(self, minutes: float) -> None:
        time.sleep(30)
        while True:
            try:
                self.update_now()
            except Exception as exc:
                log.warning("Update check failed: %s", exc)
            time.sleep(max(5.0, minutes) * 60)

    def running_commit(self) -> str | None:
        import json
        b = self.current
        try:
            c = http.client.HTTPConnection("127.0.0.1", b.port, timeout=3)
            c.request("GET", "/api/version")
            return json.loads(c.getresponse().read()).get("commit")
        except Exception:
            return None

    def update_now(self) -> dict:
        from .. import updater
        res = updater.check_and_update(self.ws)
        # Also switch when the code on disk changed some other way (e.g. a daily run installed it).
        if res.get("updated") or (self.current and updater.installed() != self.running_commit()):
            res["switched"] = self.swap()
        return res

    def handler(self):
        door = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                import json
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _proxy(self):
                from .. import updater
                if self.path == "/api/update" and self.command == "GET":
                    return self._json({**updater.status(), "last_switch": door.last_switch})
                if self.path == "/api/update" and self.command == "POST":
                    threading.Thread(target=door.update_now, daemon=True).start()
                    return self._json({"ok": True, "started": True})
                b = door.current
                if b is None:
                    return self._json({"error": "starting"}, 503)
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
                with b.lock:
                    b.active += 1
                try:
                    c = http.client.HTTPConnection("127.0.0.1", b.port, timeout=600)
                    c.request(self.command, self.path, body=body, headers=headers)
                    r = c.getresponse()
                    data = r.read()
                except Exception as exc:
                    return self._json({"error": f"review app not reachable: {exc}"}, 502)
                finally:
                    with b.lock:
                        b.active -= 1
                self.send_response(r.status)
                for k, v in r.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy

        return H

    def serve(self, open_browser: bool = True) -> None:
        from ..settings import load_settings
        self.current = self.start_backend()
        srv = ThreadingHTTPServer(("127.0.0.1", self.port), self.handler())
        srv.daemon_threads = True
        cfg = load_settings().get("update") or {}
        if cfg.get("enabled", True):
            threading.Thread(target=self.update_loop, args=(float(cfg.get("check_minutes", 60)),), daemon=True).start()
        url = f"http://127.0.0.1:{self.port}"
        if open_browser:
            import webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        print(f"\nReview page: {url}  (press Ctrl+C here to stop it). It updates itself without going offline.\n")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
            if self.current:
                self.current.stop(grace=0)


def already_running(port: int = 8000) -> bool:
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
        c.request("GET", "/api/version")
        return c.getresponse().status == 200
    except Exception:
        return False


def serve(ws, open_browser: bool = True, port: int = 8000) -> None:
    if already_running(port):  # one review page; just show it
        print(f"The review page is already running at http://127.0.0.1:{port}")
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")
        return
    FrontDoor(ws, port).serve(open_browser)
