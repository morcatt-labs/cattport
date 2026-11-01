#!/usr/bin/env python3
"""Local UI preview: the real cattport_core API, with DSM's answers simulated. Add ?docker=missing to the DSM stub via DOCKER env."""
import http.server
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "package" / "lib"))
sys.path.insert(0, str(ROOT))
import build_spk
import cattport_core as core

WORK = Path(tempfile.mkdtemp(prefix="cattport-dev-"))
UI = WORK / "target" / "ui"
shutil.copytree(ROOT / "src" / "package" / "ui", UI)
for path in UI.glob("*.js"):
    path.write_text(path.read_text(encoding="utf-8").replace("__VERSION__", build_spk.VERSION), encoding="utf-8")
(UI / "images").mkdir()
for size in build_spk.ICON_SIZES:
    (UI / "images" / ("cattport_%d.png" % size)).write_bytes(build_spk.png(build_spk.draw_icon(size)))
PATHS = core.Paths(str(WORK / "target"), str(WORK / "var"))
DOCKER = os.environ.get("DOCKER", "running")
AUTH = "ok"
CONTAINERS = [
    {"name": "jellyfin", "status": "running", "port_bindings": [{"host_port": 8096}]},
    {"name": "paperless_web", "status": "running", "port_bindings": [{"host_port": 8000}]},
    {"name": "uptime-kuma", "status": "running", "port_bindings": [{"host_port": 3001}]},
    {"name": "audiobookshelf", "status": "stopped", "port_bindings": [{"host_port": 13378}, {"host_port": 13379}]},
    {"name": "postgres", "status": "running"},
]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI), **kwargs)

    def log_message(self, *args):
        pass

    def send_json(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/dev/"):
            # Lets a test switch the simulated DSM into a failure state.
            global DOCKER, AUTH
            _, _, kind, value = self.path.split("/")
            if kind == "docker":
                DOCKER = value
            else:
                AUTH = value
            return self.send_json({"ok": True})
        if self.path.startswith("/webman/login.cgi"):
            return self.send_json({"SynoToken": "dev"})
        return super().do_GET()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/webapi/"):
            form = dict(urllib.parse.parse_qsl(body.decode()))
            api, method = form.get("api"), form.get("method")
            if api == "SYNO.Core.Package":
                packages = [] if DOCKER == "missing" else [{"id": "ContainerManager", "additional": {"status": DOCKER}}]
                return self.send_json({"success": True, "data": {"packages": packages}})
            if api == "SYNO.Docker.Container":
                if DOCKER != "running":
                    return self.send_json({"success": False, "error": {"code": 102}})
                data = {"containers": CONTAINERS} if method == "list" else {"details": {}, "profile": {}}
            elif api == "SYNO.Core.System":
                data = {"firmware_ver": "DSM 7.4.1-90080"}
            elif api == "SYNO.FileStation.Search":
                data = {"taskid": "t1"} if method == "start" else {"finished": True, "files": [{"name": "found-logo.png", "path": "/docker/icons/found-logo.png", "isdir": False}]}
            elif method == "list_share":
                data = {"shares": [{"name": "docker", "path": "/docker"}]}
            elif method == "list":
                data = {"files": [{"name": "logo.png", "path": form["folder_path"] + "/logo.png", "isdir": False}, {"name": "notes.txt", "path": form["folder_path"] + "/notes.txt", "isdir": False}, {"name": "icons", "path": form["folder_path"] + "/icons", "isdir": True}]}
            else:
                data = {}
            return self.send_json({"success": True, "data": data})
        if AUTH != "ok":
            return self.send_json({"success": False, "error": AUTH})
        request = json.loads(body)
        try:
            return self.send_json({"success": True, "data": core.handle(PATHS, request["action"], request)})
        except core.ApiError as error:
            return self.send_json({"success": False, "error": str(error)})


if __name__ == "__main__":
    print("http://localhost:8766/index.html", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", 8766), Handler).serve_forever()
