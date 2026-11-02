import base64
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "package" / "lib"))
sys.path.insert(0, str(ROOT))
import build_spk
import cattport_core as core


def png_b64(size):
    buffer = io.BytesIO()
    Image.new("RGBA", (size, size), (1, 2, 3, 255)).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


ICONS = {str(size): png_b64(size) for size in core.ICON_SIZES}


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = core.Paths(os.path.join(self.temp.name, "target"), os.path.join(self.temp.name, "var"))
        os.makedirs(self.paths.images)
        os.makedirs(self.paths.var)
        Path(self.paths.images, "cattport_16.png").write_bytes(b"x")

    def add(self, name="Immich", url="http://nas.local:2283", **extra):
        return core.save_app(self.paths, {"key": None, "app": dict(name=name, url=url, **extra), "icons": ICONS})

    def config(self):
        return json.loads(Path(self.paths.config).read_text())

    def test_publish_writes_app_and_tile_and_clears_dirty(self):
        self.assertEqual(self.add()["pending"], 1)
        result = core.publish(self.paths, restarted=True)
        tile = core.TILE_PREFIX + "Immich"
        self.assertEqual(result["ids"], [tile])
        self.assertEqual(result["state"]["pending"], 0)
        config = self.config()
        self.assertEqual(config[".url"][tile]["url"], "http://nas.local:2283")
        self.assertFalse(config[".url"][tile]["allUsers"])
        self.assertFalse(config["cattport.js"][core.NAMESPACE + ".Instance"]["allUsers"])

    def test_every_published_config_declares_the_version(self):
        Path(self.paths.version).parent.mkdir(parents=True, exist_ok=True)
        Path(self.paths.version).write_text("9.9")
        core.publish(self.paths)
        entries = self.config()["cattport.js"]
        self.assertEqual({entry["version"] for entry in entries.values()}, {"9.9"})

    def test_follow_tile_uses_dsm_address(self):
        self.add(url="http://192.168.1.2:2283", follow=True)
        core.publish(self.paths)
        entry = self.config()[".url"][core.TILE_PREFIX + "Immich"]
        self.assertEqual((entry["url"], entry["port"], entry["protocol"]), ("/", 2283, "http"))

    def test_follow_is_ignored_for_a_path(self):
        self.add(url="http://192.168.1.2:2283/photos", follow=True)
        core.publish(self.paths)
        self.assertEqual(self.config()[".url"][core.TILE_PREFIX + "Immich"]["url"], "http://192.168.1.2:2283/photos")

    def test_hidden_app_has_no_tile(self):
        self.add(hidden=True)
        self.assertEqual(core.publish(self.paths)["ids"], [])

    def test_delete_and_undo(self):
        self.add()
        core.publish(self.paths)
        state = core.delete_app(self.paths, {"key": "Immich"})
        self.assertTrue(state["canUndo"])
        core.publish(self.paths)
        self.assertNotIn(".url", self.config())
        self.assertEqual(os.listdir(self.paths.images), ["cattport_16.png"])
        state = core.undo_delete(self.paths)
        self.assertEqual([app["key"] for app in state["apps"]], ["Immich"])
        core.publish(self.paths)
        self.assertIn(core.TILE_PREFIX + "Immich", self.config()[".url"])

    def test_duplicate_names_get_distinct_keys_and_edit_keeps_id(self):
        self.add()
        state = self.add()
        self.assertEqual([app["key"] for app in state["apps"]], ["Immich", "Immich2"])
        state = core.save_app(self.paths, {"key": "Immich", "app": {"name": "Photos", "url": "https://example.com"}})
        self.assertEqual(state["apps"][0]["id"], core.TILE_PREFIX + "Immich")
        self.assertEqual(state["apps"][0]["name"], "Photos")

    def test_rejects_unsafe_urls_and_bad_icons(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd", "immich", "data:text/html,x", "http://a:bad"):
            with self.assertRaises(core.ApiError):
                self.add(url=url)
        with self.assertRaises(core.ApiError):
            core.save_app(self.paths, {"app": {"name": "A", "url": "http://a"}, "icons": dict(ICONS, **{"64": png_b64(32)})})
        with self.assertRaises(core.ApiError):
            core.save_app(self.paths, {"app": {"name": "A", "url": "http://a"}, "icons": dict(ICONS, **{"64": base64.b64encode(b"<svg></svg>").decode()})})

    def test_delete_cannot_escape_icon_directory(self):
        self.add()
        with self.assertRaises(core.ApiError):
            core.delete_app(self.paths, {"key": "../.."})
        self.assertTrue(Path(self.paths.state).exists())

    def test_fresh_install_has_no_apps_and_keeps_cattport_tile(self):
        self.assertEqual(core.public_state(self.paths)["apps"], [])
        self.assertEqual(core.publish(self.paths)["ids"], [])
        self.assertIn("cattport.js", self.config())

    def test_online_search_stays_off_until_allowed(self):
        self.assertIsNone(core.public_state(self.paths)["settings"]["online"])
        self.assertEqual(core.internet_icons(self.paths, {"name": "immich"}), {"results": [], "disabled": True})

    def test_settings_ignore_unknown_values(self):
        state = core.save_settings(self.paths, {"settings": {"theme": "dark", "language": "xx", "online": "yes"}})
        self.assertEqual(state["settings"]["theme"], "dark")
        self.assertEqual(state["settings"]["language"], "auto")
        self.assertIsNone(state["settings"]["online"])

    def test_damaged_data_restores_the_last_good_copy(self):
        self.add()
        self.add("Jellyfin")
        Path(self.paths.state).write_text("{broken")
        state = core.public_state(self.paths)
        self.assertTrue(state["restored"])
        self.assertEqual([app["key"] for app in state["apps"]], ["Immich"])

    def test_damaged_data_without_copy_is_reported_and_kept(self):
        Path(self.paths.state).write_text("{broken")
        with self.assertRaises(core.ApiError):
            core.load_state(self.paths)
        self.assertEqual(Path(self.paths.state).read_text(), "{broken")

    def test_newer_data_is_refused_untouched(self):
        Path(self.paths.state).write_text(json.dumps({"schema": core.SCHEMA + 1, "apps": []}))
        with self.assertRaisesRegex(core.ApiError, "newer_data"):
            core.load_state(self.paths)

    def test_export_import_round_trip(self):
        self.add(allUsers=True)
        backup = json.loads(json.dumps(core.export_data(self.paths)))
        core.delete_app(self.paths, {"key": "Immich"})
        state = core.import_data(self.paths, {"backup": backup})
        self.assertTrue(state["apps"][0]["allUsers"])
        self.assertEqual(len(core.import_data(self.paths, {"backup": backup})["apps"]), 1)

    def test_bad_backup_changes_nothing(self):
        self.add()
        bad = {"format": "cattport-backup", "apps": [{"app": {"name": "X", "url": "javascript:1"}, "icons": ICONS}]}
        with self.assertRaises(core.ApiError):
            core.import_data(self.paths, {"backup": bad})
        with self.assertRaises(core.ApiError):
            core.import_data(self.paths, {"backup": {"format": "other"}})
        self.assertEqual(len(core.public_state(self.paths)["apps"]), 1)

    def test_a_missing_icon_does_not_stop_publishing(self):
        Path(self.paths.images, "cattport_16.png").unlink()
        for size in core.ICON_SIZES:
            Path(self.paths.images, "cattport_%d.png" % size).write_bytes(base64.b64decode(ICONS[str(size)]))
        self.add()
        self.add("Jellyfin")
        shutil.rmtree(os.path.join(self.paths.icons, "Immich"))
        self.assertEqual(len(core.publish(self.paths)["ids"]), 2)
        self.assertTrue(core.icon_preview(self.paths, {"key": "Immich"})["dataUrl"])
        self.assertEqual(len(core.export_data(self.paths)["apps"]), 2)

    def test_parallel_changes_are_not_lost(self):
        script = (
            "import sys; sys.path.insert(0, %r); import cattport_core as core, json;"
            "paths = core.Paths(%r, %r);"
            "core.handle(paths, 'save_settings', {'settings': {'welcomed': True}})" % (str(ROOT / "src/package/lib"), self.paths.target, self.paths.var)
        )
        self.add()
        adders = [threading.Thread(target=core.handle, args=(self.paths, "save_app", {"app": {"name": "App%d" % index, "url": "http://a"}, "icons": ICONS})) for index in range(12)]
        for thread in adders:
            thread.start()
        subprocess.run([sys.executable, "-c", script], check=True)
        for thread in adders:
            thread.join()
        state = core.public_state(self.paths)
        self.assertEqual(len(state["apps"]), 13)
        self.assertTrue(state["settings"]["welcomed"])

    def test_start_keeps_published_tiles_when_data_is_unreadable(self):
        self.add()
        core.publish(self.paths)
        Path(self.paths.state).write_text("{broken")
        Path(self.paths.backup).write_text("{broken")
        env = dict(os.environ, SYNOPKG_PKGDEST=self.paths.target, SYNOPKG_PKGVAR=self.paths.var)
        subprocess.run([sys.executable, str(ROOT / "src/package/lib/cattport_start.py")], env=env, check=True, stderr=subprocess.DEVNULL)
        self.assertIn(core.TILE_PREFIX + "Immich", self.config()[".url"])

    def test_package_start_records_what_other_users_see(self):
        self.add(allUsers=True)
        self.assertEqual(core.publish(self.paths)["state"]["pending"], 1)
        env = dict(os.environ, SYNOPKG_PKGDEST=self.paths.target, SYNOPKG_PKGVAR=self.paths.var)
        subprocess.run([sys.executable, str(ROOT / "src/package/lib/cattport_start.py")], env=env, check=True)
        self.assertEqual(core.public_state(self.paths)["pending"], 0)

    def test_start_rebuilds_a_missing_config_when_data_is_unreadable(self):
        Path(self.paths.state).write_text("{broken")
        env = dict(os.environ, SYNOPKG_PKGDEST=self.paths.target, SYNOPKG_PKGVAR=self.paths.var)
        subprocess.run([sys.executable, str(ROOT / "src/package/lib/cattport_start.py")], env=env, check=True, stderr=subprocess.DEVNULL)
        self.assertIn("cattport.js", self.config())
        self.assertEqual(Path(self.paths.state).read_text(), "{broken")

    def test_changes_are_counted_until_saved_to_dsm(self):
        self.assertEqual(self.add("Grafana")["pending"], 1)
        self.assertEqual(self.add("Immich", allUsers=True)["pending"], 2)
        self.assertEqual(core.publish(self.paths)["state"]["pending"], 2)
        self.assertEqual(core.publish(self.paths, restarted=True)["state"]["pending"], 0)

    def test_every_kind_of_tile_change_is_counted_once(self):
        self.add("Immich", allUsers=True)
        core.publish(self.paths, restarted=True)
        url = "http://nas.local:2283"
        for app in ({"name": "Photos"}, {"allUsers": False}, {"hidden": True}, {"external": "https://p.example.com"}):
            state = core.save_app(self.paths, {"key": "Immich", "app": dict({"name": "Immich", "url": url, "allUsers": True}, **app)})
            self.assertEqual(state["pending"], 1, app)
            core.save_app(self.paths, {"key": "Immich", "app": {"name": "Immich", "url": url, "allUsers": True}})
        self.assertEqual(core.delete_app(self.paths, {"key": "Immich"})["pending"], 1)

    def test_undoing_a_change_leaves_nothing_to_save(self):
        self.add()
        core.publish(self.paths, restarted=True)
        core.delete_app(self.paths, {"key": "Immich"})
        self.assertEqual(core.undo_delete(self.paths)["pending"], 0)

    def test_settings_are_not_counted(self):
        self.add()
        core.publish(self.paths, restarted=True)
        self.assertEqual(core.save_settings(self.paths, {"settings": {"theme": "dark"}})["pending"], 0)

    def test_saving_in_the_editor_does_not_touch_dsm(self):
        self.add()
        self.assertFalse(Path(self.paths.config).exists())

    def test_hidden_tile_is_gone_for_everyone_and_keeps_its_audience(self):
        self.add(allUsers=True, hidden=True)
        core.publish(self.paths, restarted=True)
        self.assertNotIn(".url", self.config())
        state = core.save_app(self.paths, {"key": "Immich", "app": {"name": "Immich", "url": "http://nas.local:2283", "allUsers": True}})
        self.assertTrue(state["apps"][0]["allUsers"])
        core.publish(self.paths, restarted=True)
        self.assertTrue(self.config()[".url"][core.TILE_PREFIX + "Immich"]["allUsers"])

    def test_who_sees_this_reaches_the_tile(self):
        self.add("Immich", allUsers=True)
        self.add("Grafana", allUsers=False)
        core.publish(self.paths, restarted=True)
        tiles = self.config()[".url"]
        self.assertTrue(tiles[core.TILE_PREFIX + "Immich"]["allUsers"])
        self.assertFalse(tiles[core.TILE_PREFIX + "Grafana"]["allUsers"])

    def test_everyone_tile_stays_visible_to_administrators(self):
        self.add(allUsers=True)
        core.publish(self.paths)
        entry = self.config()[".url"][core.TILE_PREFIX + "Immich"]
        self.assertTrue(entry["allUsers"])
        self.assertNotIn("grantPrivilege", entry)

    def test_outside_address_goes_through_the_opener(self):
        self.add("Jellyfin", url="http://192.168.1.2:1802", follow=True, external="https://cinema.example.com")
        self.assertEqual(core.publish(self.paths, restarted=True)["state"]["pending"], 0)
        entry = self.config()[".url"][core.TILE_PREFIX + "Jellyfin"]
        self.assertEqual(entry["url"], "webman/3rdparty/Cattport/open.html?app=Jellyfin")
        self.assertNotIn("port", entry)
        link = json.loads(Path(self.paths.ui, "links.json").read_text())["Jellyfin"]
        self.assertEqual((link["external"], link["port"], link["protocol"]), ("https://cinema.example.com", 1802, "http"))

    def test_changing_only_the_outside_address_needs_publishing(self):
        self.add(external="https://a.example.com")
        core.publish(self.paths)
        state = core.save_app(self.paths, {"key": "Immich", "app": {"name": "Immich", "url": "http://nas.local:2283", "external": "https://b.example.com"}})
        self.assertEqual(state["pending"], 1)

    def test_unsafe_outside_address_is_refused(self):
        with self.assertRaises(core.ApiError):
            self.add(external="javascript:alert(1)")

    def test_slugs(self):
        self.assertEqual(core.name_slugs("IMMICH"), ["immich"])
        self.assertEqual(core.name_slugs("Immich Server")[:2], ["immich-server", "immich"])
        self.assertEqual(core.name_slugs("  "), [])


class Handler(http.server.BaseHTTPRequestHandler):
    PNG = base64.b64decode(png_b64(16))

    def do_GET(self):
        if self.path == "/to-ftp":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1/secret.png")
            self.end_headers()
            return
        if self.path == "/":
            body, kind = b'<html><link rel="shortcut icon" href="/a.png"></html>', "text/html"
        elif self.path == "/a.png":
            body, kind = self.PNG, "image/png"
        elif self.path == "/not-image":
            body, kind = b"<html>nope</html>", "image/png"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_docker_icon_comes_from_container_page(self):
        results = core.docker_icons({"ports": [self.port], "labels": {}})["results"]
        self.assertEqual(results[0]["source"], "docker")
        self.assertTrue(results[0]["dataUrl"].startswith("data:image/png;base64,"))

    def test_a_silent_local_port_gives_up_quickly(self):
        silent = socket.socket()
        self.addCleanup(silent.close)
        silent.bind(("127.0.0.1", 0))
        silent.listen(8)
        started = time.time()
        results = core.docker_icons({"ports": [silent.getsockname()[1]], "labels": {}})["results"]
        self.assertEqual(results, [])
        self.assertLess(time.time() - started, 10)

    def test_content_type_is_not_trusted(self):
        with self.assertRaises(core.ApiError):
            core.fetch_image("http://127.0.0.1:%d/not-image" % self.port)

    def test_address_check(self):
        self.assertTrue(core.check_address({"url": "http://127.0.0.1:%d/" % self.port})["reachable"])
        self.assertTrue(core.check_address({"url": "http://127.0.0.1:%d/missing" % self.port})["reachable"])
        self.assertFalse(core.check_address({"url": "http://127.0.0.1:1/"})["reachable"])
        self.assertFalse(core.check_address({"url": "file:///etc/passwd"})["reachable"])

    def test_redirect_cannot_leave_http(self):
        with self.assertRaises(core.ApiError):
            core.fetch_image("http://127.0.0.1:%d/to-ftp" % self.port)

    def test_rejects_non_http_and_bad_ports(self):
        with self.assertRaises(core.ApiError):
            core.fetch_image("file:///etc/passwd")
        with self.assertRaises(core.ApiError):
            core.docker_icons({"ports": ["1;rm"], "labels": {}})


class TextTest(unittest.TestCase):
    def load_texts(self):
        texts = {}
        for path in (ROOT / "src" / "package" / "ui" / "text").glob("*.js"):
            source = path.read_text(encoding="utf-8")
            texts[path.stem] = json.loads(source[source.index("= {") + 2:source.rindex("}") + 1])
        return texts

    def test_every_dsm_language_is_complete(self):
        import re
        texts = self.load_texts()
        self.assertEqual(len(texts), 22)
        self.assertEqual(set(texts) | {"auto"}, set(core.LANGUAGES))
        script = (ROOT / "src" / "package" / "ui" / "app.js").read_text(encoding="utf-8")
        for code in re.findall(r'\w+: "([\w-]+)"', script[script.index("DSM_LANGUAGES ="):script.index("LANGUAGE_NAMES =")]):
            self.assertIn(code, texts)
        for code, text in texts.items():
            self.assertEqual(set(text), set(texts["en"]), code)
            for key, value in text.items():
                self.assertEqual(set(re.findall(r"\{\w+\}", value)), set(re.findall(r"\{\w+\}", texts["en"][key])), code + " " + key)
                self.assertTrue(value.strip(), code + " " + key)

    def test_all_used_keys_exist(self):
        ui = ROOT / "src" / "package" / "ui"
        source = (ui / "text" / "en.js").read_text(encoding="utf-8")
        import re
        used = set(re.findall(r'data-i18n(?:-placeholder|-label)?="([^"]+)"', (ui / "index.html").read_text(encoding="utf-8")))
        missing = [key for key in used if '"%s"' % key not in source]
        self.assertEqual(missing, [])


class PackageTest(unittest.TestCase):
    def test_spk_layout(self):
        with tarfile.open(build_spk.build()) as spk:
            names = set(spk.getnames())
            self.assertLessEqual({"INFO", "package.tgz", "conf/privilege", "scripts/start-stop-status", "scripts/postuninst", "WIZARD_UIFILES/uninstall_uifile", "LICENSE", "PACKAGE_ICON_256.PNG"}, names)
            info = spk.extractfile("INFO").read().decode()
            self.assertIn('dsmappname="labs.morcatt.cattport.Instance"', info)
            self.assertIn('maintainer="Morcatt Labs"', info)
            # Open to every DSM 7 and to later versions; there must be no upper limit.
            self.assertIn('os_min_ver="7.0-40000"', info)
            self.assertNotIn("os_max_ver", info)
            for name in ("WIZARD_UIFILES/uninstall_uifile", "WIZARD_UIFILES/uninstall_uifile_rum"):
                json.loads(spk.extractfile(name).read())
            inner = tarfile.open(fileobj=io.BytesIO(spk.extractfile("package.tgz").read()))
            members = {item.name: item for item in inner.getmembers()}
            self.assertEqual(members["ui/api.cgi"].mode & 0o111, 0o111)
            self.assertGreater(members["ui/cattport.js"].mtime, 0)
            self.assertIn("ui/images/cattport_256.png", members)
            self.assertNotIn("ui/config", members)
            # DSM loads <package>/style.css across its whole interface.
            self.assertNotIn("ui/style.css", members)
            self.assertIn("ui/open.html", members)
            # Without a version in the address, browsers keep old files after an upgrade.
            page = inner.extractfile("ui/index.html").read().decode()
            for name in ("app.js", "network.js", "text/en.js", "page.css"):
                self.assertIn('%s?v=%s"' % (name, build_spk.VERSION), page)
            self.assertIn("ui/network.js", members)
            self.assertEqual(len([name for name in members if name.startswith("ui/text/")]), 22)
            for item in inner.getmembers():
                if item.isfile():
                    data = inner.extractfile(item).read()
                    self.assertNotIn(b"__VERSION__", data, item.name)
            self.assertIn(build_spk.VERSION.encode(), inner.extractfile("ui/cattport.js").read())
            self.assertEqual(inner.extractfile("lib/version").read().decode(), build_spk.VERSION)

    def test_icon_keeps_dsm_margin(self):
        box = build_spk.draw_icon(256).getbbox()
        self.assertGreater(box[0], 8)
        self.assertLess(box[2], 248)


if __name__ == "__main__":
    unittest.main()
