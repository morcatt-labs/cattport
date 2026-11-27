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
        # The address stays out of DSM's files: the tile goes through the opener.
        self.assertEqual(config[".url"][tile]["url"], "webman/3rdparty/Cattport/open.html?app=Immich")
        self.assertNotIn("nas.local", json.dumps(config))
        self.assertFalse(config[".url"][tile]["allUsers"])
        self.assertFalse(config["cattport.js"][core.NAMESPACE + ".Instance"]["allUsers"])

    def test_every_published_config_declares_the_version(self):
        Path(self.paths.version).parent.mkdir(parents=True, exist_ok=True)
        Path(self.paths.version).write_text("9.9")
        core.publish(self.paths)
        entries = self.config()["cattport.js"]
        self.assertEqual({entry["version"] for entry in entries.values()}, {"9.9"})

    def opened(self, key="Immich", user="anna", groups=(), admin=False):
        return core.open_link(self.paths, {"key": key}, {"user": user, "groups": list(groups), "admin": admin})

    def test_follow_tile_uses_dsm_address(self):
        self.add(url="http://192.168.1.2:2283", follow=True)
        core.publish(self.paths)
        link = self.opened(admin=True)
        self.assertEqual((link["port"], link["protocol"]), (2283, "http"))

    def test_follow_is_ignored_for_a_path(self):
        self.add(url="http://192.168.1.2:2283/photos", follow=True)
        core.publish(self.paths)
        self.assertEqual(self.opened(admin=True), {"url": "http://192.168.1.2:2283/photos", "external": ""})

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
        link = self.opened("Jellyfin", admin=True)
        self.assertEqual((link["external"], link["port"], link["protocol"]), ("https://cinema.example.com", 1802, "http"))
        # Nothing about where tiles go is left where anyone could download it.
        self.assertFalse(Path(self.paths.ui, "links.json").exists())

    def test_old_public_links_file_is_removed(self):
        Path(self.paths.ui, "links.json").write_text("{}")
        core.publish(self.paths)
        self.assertFalse(Path(self.paths.ui, "links.json").exists())

    def test_admins_only_tile_opens_only_for_admins(self):
        self.add()
        core.publish(self.paths)
        self.assertEqual(self.opened(admin=True)["url"], "http://nas.local:2283")
        with self.assertRaisesRegex(core.ApiError, "not_allowed"):
            self.opened(user="anna", groups=["users"])

    def test_everyone_tile_opens_for_everyone(self):
        self.add(audience="everyone")
        core.publish(self.paths)
        self.assertEqual(self.opened(user="anna", groups=["users"])["url"], "http://nas.local:2283")
        entry = self.config()[".url"][core.TILE_PREFIX + "Immich"]
        self.assertTrue(entry["allUsers"])
        self.assertNotIn("grantPrivilege", entry)

    def test_chosen_people_see_and_open_the_tile(self):
        self.add(audience="people", people={"users": ["Anna"], "groups": ["family"]})
        result = core.publish(self.paths)
        entry = self.config()[".url"][core.TILE_PREFIX + "Immich"]
        self.assertFalse(entry["allUsers"])
        self.assertEqual(entry["grantPrivilege"], "all")
        self.assertEqual(result["rules"], {core.TILE_PREFIX + "Immich": {"mode": "people", "users": ["Anna"], "groups": ["family"]}})
        self.assertTrue(self.opened(user="anna"))
        self.assertTrue(self.opened(user="bob", groups=["users", "Family"]))
        with self.assertRaisesRegex(core.ApiError, "not_allowed"):
            self.opened(user="bob", groups=["users"])
        self.assertTrue(self.opened(user="bob", admin=True))

    def test_everyone_except_some_people(self):
        self.add(audience="everyone", people={"users": ["bob"], "groups": ["kids"]})
        result = core.publish(self.paths)
        self.assertEqual(result["rules"][core.TILE_PREFIX + "Immich"]["mode"], "everyone")
        self.assertEqual(self.config()[".url"][core.TILE_PREFIX + "Immich"]["grantPrivilege"], "all")
        self.assertTrue(self.opened(user="anna", groups=["users"]))
        for user, groups in (("bob", ["users"]), ("carl", ["users", "kids"])):
            with self.assertRaisesRegex(core.ApiError, "not_allowed"):
                self.opened(user=user, groups=groups)

    def test_tiles_without_people_clear_earlier_privileges(self):
        self.add(audience="everyone", people={"users": ["bob"]})
        self.add("Grafana", audience="people", people={"users": ["anna"]})
        self.add("Old", audience="people", people={"users": ["anna"]})
        core.publish(self.paths, restarted=True)
        core.save_app(self.paths, {"key": "Immich", "app": {"name": "Immich", "url": "http://nas.local:2283", "audience": "everyone"}})
        core.save_app(self.paths, {"key": "Grafana", "app": {"name": "Grafana", "url": "http://nas.local:2283", "audience": "admins"}})
        core.delete_app(self.paths, {"key": "Old"})
        rules = core.publish(self.paths)["rules"]
        none = {"mode": "none", "users": [], "groups": []}
        self.assertEqual(rules, {core.TILE_PREFIX + key: none for key in ("Immich", "Grafana", "Old")})

    def test_unwritten_privileges_stay_unsaved(self):
        self.add(audience="people", people={"users": ["anna"]})
        result = core.publish(self.paths, restarted=True)
        self.assertEqual(result["state"]["pending"], 1)
        self.assertEqual(core.rules_applied(self.paths, {"fingerprint": "stale"})["pending"], 1)
        self.assertEqual(core.handle(self.paths, "rules_applied", {"fingerprint": result["fingerprint"]})["pending"], 0)
        # Plain tiles need no privileges at all.
        core.save_app(self.paths, {"key": "Immich", "app": {"name": "Immich", "url": "http://nas.local:2283", "audience": "everyone"}})
        result = core.publish(self.paths, restarted=True)
        self.assertEqual((result["fingerprint"], result["state"]["pending"]), ("", 0))

    def test_everyone_except_leaves_out_accounts_dsm_leaves_out(self):
        self.add(audience="everyone", people={"users": ["bob"], "groups": ["CORP\\Sales"]})
        core.publish(self.paths)
        # A domain account isn't in the local "users" group, which is all DSM allows here.
        with self.assertRaisesRegex(core.ApiError, "not_allowed"):
            self.opened(user="CORP\\eve", groups=["CORP\\Domain Users"])

    def test_old_unsafe_names_are_cleaned_for_dsm(self):
        Path(self.paths.state).write_text(json.dumps({"apps": [{"key": "A", "name": "<b>A</b>", "desc": 'x"y', "url": "http://a", "allUsers": True}]}))
        entry = core.tile_entry(core.load_state(self.paths)["apps"][0])
        self.assertEqual((entry["title"], entry["desc"]), ("bA/b", "xy"))

    def test_people_are_unique_whatever_the_case(self):
        state = self.add(audience="people", people={"users": ["anna", "Anna"]})
        self.assertEqual(state["apps"][0]["people"]["users"], ["anna"])

    def test_chosen_people_need_someone(self):
        with self.assertRaisesRegex(core.ApiError, "people_required"):
            self.add(audience="people", people={"users": [], "groups": []})

    def test_admins_only_drops_the_people(self):
        state = self.add(audience="admins", people={"users": ["anna"], "groups": []})
        self.assertEqual(state["apps"][0]["people"], {"users": [], "groups": []})
        self.assertFalse(state["apps"][0]["allUsers"])

    def test_bad_people_are_refused(self):
        for people in ({"users": ["<b>x</b>"]}, {"users": "anna"}, {"groups": [1]}, {"users": [""]}, {"users": [" anna"]}, ["anna"],
                       {"users": ["u%d" % index for index in range(core.MAX_PEOPLE + 1)]}):
            with self.assertRaisesRegex(core.ApiError, "invalid_people", msg=people):
                self.add(audience="people", people=people)

    def test_domain_and_ldap_accounts_are_accepted(self):
        state = self.add(audience="people", people={"users": ["CORP\\anna", "bob@ldap.example"], "groups": ["Domain Users"]})
        self.assertEqual(state["apps"][0]["people"]["users"], ["bob@ldap.example", "CORP\\anna"])

    def test_old_apps_keep_their_audience(self):
        Path(self.paths.state).write_text(json.dumps({"apps": [
            {"key": "A", "name": "A", "url": "http://a", "allUsers": True},
            {"key": "B", "name": "B", "url": "http://b", "allUsers": False}]}))
        for size in core.ICON_SIZES:
            Path(self.paths.images, "cattport_%d.png" % size).write_bytes(base64.b64decode(ICONS[str(size)]))
        core.publish(self.paths)
        tiles = self.config()[".url"]
        self.assertTrue(tiles[core.TILE_PREFIX + "A"]["allUsers"])
        self.assertFalse(tiles[core.TILE_PREFIX + "B"]["allUsers"])
        self.assertTrue(self.opened("A", user="anna"))
        with self.assertRaises(core.ApiError):
            self.opened("B", user="anna")

    def test_hidden_or_unsaved_tiles_do_not_open(self):
        self.add(audience="everyone", hidden=True)
        self.add("Grafana", audience="everyone")
        core.publish(self.paths)
        self.add("Later", audience="everyone")
        for key in ("Immich", "Later", "../apps", "__proto__", None, ["Grafana"]):
            with self.assertRaisesRegex(core.ApiError, "app_gone", msg=key):
                self.opened(key, admin=True)
        self.assertTrue(self.opened("Grafana"))

    def test_changing_who_sees_a_tile_needs_saving(self):
        self.add(audience="people", people={"users": ["anna"]})
        core.publish(self.paths, restarted=True)
        state = core.save_app(self.paths, {"key": "Immich", "app": {"name": "Immich", "url": "http://nas.local:2283", "audience": "people", "people": {"users": ["anna", "bob"]}}})
        self.assertEqual(state["pending"], 1)

    def test_markup_in_names_is_refused(self):
        for name in ("<img src=x onerror=alert(1)>", 'Say "hi"', "a`b", "tab\there"):
            with self.assertRaisesRegex(core.ApiError, "unsafe_text", msg=name):
                self.add(name=name)
        with self.assertRaisesRegex(core.ApiError, "unsafe_text"):
            self.add(desc="<script>")
        self.assertEqual(self.add(name="Tom & Jerry's")["apps"][0]["name"], "Tom & Jerry's")

    def test_long_or_odd_addresses_are_refused(self):
        for url in ("http://a/" + "x" * core.MAX_URL, 'http://a/"><script>', "http://a b/", "http://a/\x00"):
            with self.assertRaisesRegex(core.ApiError, "invalid_url", msg=url):
                self.add(url=url)
        with self.assertRaisesRegex(core.ApiError, "invalid_url"):
            self.add(external="https://x/<svg>")

    def test_import_that_does_not_fit_leaves_no_icons(self):
        state = core.load_state(self.paths)
        state["apps"] = [{"key": "K%d" % index, "name": "K%d" % index, "url": "http://a"} for index in range(core.MAX_APPS - 1)]
        core.save_state(self.paths, state)
        backup = {"format": "cattport-backup", "apps": [{"app": {"name": n, "url": "http://a"}, "icons": ICONS} for n in ("X", "Y")]}
        with self.assertRaisesRegex(core.ApiError, "too_many_apps"):
            core.import_data(self.paths, {"backup": backup})
        self.assertFalse(Path(self.paths.icons).exists())

    def test_import_refuses_odd_shapes(self):
        for backup in ({"format": "cattport-backup", "schema": "2", "apps": []}, {"format": "cattport-backup", "apps": ["x"]}):
            with self.assertRaisesRegex(core.ApiError, "invalid_backup"):
                core.import_data(self.paths, {"backup": backup})

    def test_export_keeps_who_sees_each_app(self):
        self.add(audience="people", people={"users": ["anna"], "groups": ["family"]})
        backup = json.loads(json.dumps(core.export_data(self.paths)))
        core.delete_app(self.paths, {"key": "Immich"})
        app = core.import_data(self.paths, {"backup": backup})["apps"][0]
        self.assertEqual((app["audience"], app["people"]), ("people", {"users": ["anna"], "groups": ["family"]}))

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


def load_api():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("cattport_api", str(ROOT / "src" / "package" / "ui" / "api.cgi"))
    spec = importlib.util.spec_from_loader("cattport_api", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ApiTest(unittest.TestCase):
    """api.cgi with DSM's sign-in replaced: who is signed in decides what may be done."""

    def setUp(self):
        self.api = load_api()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = core.Paths(os.path.join(self.temp.name, "target"), os.path.join(self.temp.name, "var"))
        os.makedirs(self.paths.images)
        Path(self.paths.images, "cattport_16.png").write_bytes(b"x")
        core.save_app(self.paths, {"app": {"name": "Immich", "url": "http://nas.local:2283", "audience": "people", "people": {"users": ["anna"]}}, "icons": ICONS})
        core.publish(self.paths)
        self.user = "admin"
        self.groups = {"admin": ["administrators", "users"], "anna": ["users"], "bob": ["users"]}
        self.api.signed_in_user = lambda: self.user
        self.api.groups_of = lambda user: self.groups.get(user, [])
        from unittest import mock
        original = core.Paths
        patcher = mock.patch.object(self.api.core, "Paths", lambda *args: original(*args) if args else self.paths)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, body, method="POST", origin=None, kind="application/json"):
        data = json.dumps(body).encode()
        env = {"REQUEST_METHOD": method, "CONTENT_TYPE": kind, "CONTENT_LENGTH": str(len(data)), "HTTP_HOST": "nas:5001"}
        if origin:
            env["HTTP_ORIGIN"] = origin
        out = io.BytesIO()
        stdin = io.TextIOWrapper(io.BytesIO(data))
        old = (dict(os.environ), sys.stdin, sys.stdout)
        os.environ.clear()
        os.environ.update(env)
        sys.stdin, sys.stdout = stdin, io.TextIOWrapper(out)
        try:
            self.api.main()
            sys.stdout.flush()
            value = out.getvalue()
        finally:
            os.environ.clear()
            os.environ.update(old[0])
            sys.stdin, sys.stdout = old[1], old[2]
        head, _, payload = value.partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0].decode(), json.loads(payload)

    def test_admin_reads_state(self):
        status, body = self.call({"action": "state"})
        self.assertEqual(status, "Status: 200 OK")
        self.assertTrue(body["success"])

    def test_signed_out_is_refused(self):
        self.user = None
        self.assertEqual(self.call({"action": "open", "key": "Immich"})[1]["error"], "session")

    def test_regular_user_can_only_open(self):
        self.user = "bob"
        for action in ("state", "save_app", "publish", "export", "fetch_image", "check_address"):
            status, body = self.call({"action": action})
            self.assertEqual((status, body["error"]), ("Status: 403 Forbidden", "not_admin"), action)
        self.assertEqual(self.call({"action": "open", "key": "Immich"})[1]["error"], "not_allowed")
        self.user = "anna"
        self.assertEqual(self.call({"action": "open", "key": "Immich"})[1]["data"]["url"], "http://nas.local:2283")

    def test_cross_site_and_form_posts_are_refused(self):
        self.assertEqual(self.call({"action": "state"}, origin="https://evil.example")[0], "Status: 405 Method Not Allowed")
        self.assertEqual(self.call({"action": "state"}, method="GET")[0], "Status: 405 Method Not Allowed")
        self.assertEqual(self.call({"action": "state"}, kind="text/plain")[0], "Status: 415 Unsupported Media Type")
        self.assertEqual(self.call({"action": "state"}, origin="https://nas:5001")[0], "Status: 200 OK")

    def test_regular_users_cannot_send_large_bodies(self):
        self.user = "anna"
        status, body = self.call({"action": "open", "key": "Immich", "pad": "x" * 5000})
        self.assertEqual(status, "Status: 413 Payload Too Large")

    def test_odd_bodies_do_not_crash(self):
        for body in ([], "x", {"action": "save_app", "app": []}, {"action": "import", "backup": {"format": "cattport-backup", "apps": [1]}}):
            status, answer = self.call(body)
            self.assertFalse(answer["success"], body)


HOST_HARNESS = r"""
const fs = require("fs");
const calls = [];
const listed = JSON.parse(process.argv[2]);
global.Ext = { ns: () => {}, define: () => {} };
global.labs = { morcatt: { cattport: {} } };
global.SYNO = { SDS: { Session: { SynoToken: "t" }, Config: { FnMap: {} }, AppLaunch: () => {}, reloadJSConfig: () => {} } };
global.setTimeout = (fn) => fn();
global.fetch = async (url, options) => {
  if (url.endsWith("api.cgi")) {
    calls.push(JSON.parse(options.body));
    return { json: async () => ({ success: true, data: { ids: [], rules: JSON.parse(process.argv[1]), fingerprint: "f1" } }) };
  }
  const form = Object.fromEntries(new URLSearchParams(options.body.toString()));
  calls.push(form);
  if (form.method === "list") return { json: async () => ({ success: true, data: { rules: listed } }) };
  return { json: async () => ({ success: true }) };
};
eval(fs.readFileSync(process.argv[3], "utf8"));
let status = null;
labs.morcatt.cattport.listen((value) => { status = value; });
labs.morcatt.cattport.saveToDsm().then(() => console.log(JSON.stringify({ status: status, calls: calls.filter((c) => c.api === "SYNO.Core.AppPriv.Rule"), cattport: calls.filter((c) => c.action) })));
"""


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class HostTest(unittest.TestCase):
    """cattport.js writes DSM's Application Privileges after Save to DSM."""

    def run_host(self, rules, listed=()):
        script = ROOT / "src" / "package" / "ui" / "cattport.js"
        out = subprocess.run(["node", "-e", HOST_HARNESS, "--", json.dumps(rules), json.dumps(list(listed)), str(script)],
                             stdout=subprocess.PIPE, check=True).stdout
        result = json.loads(out)
        for call in result["calls"]:
            if "rules" in call:
                call["rules"] = json.loads(call["rules"])
        return result

    def test_chosen_people_are_allowed(self):
        tile = core.TILE_PREFIX + "Immich"
        result = self.run_host({tile: {"mode": "people", "users": ["anna"], "groups": ["family"]}})
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["cattport"], [{"action": "publish"}, {"action": "rules_applied", "fingerprint": "f1"}])
        listed, saved = result["calls"]
        self.assertEqual((listed["method"], json.loads(listed["app_id"])), ("list", tile))
        self.assertEqual(saved["method"], "set")
        self.assertEqual(saved["rules"], [
            {"entity_type": "user", "entity_name": "anna", "app_id": tile, "allow_ip": ["0.0.0.0"], "deny_ip": []},
            {"entity_type": "group", "entity_name": "family", "app_id": tile, "allow_ip": ["0.0.0.0"], "deny_ip": []}])

    def test_everyone_except_denies_the_exceptions(self):
        tile = core.TILE_PREFIX + "Immich"
        rules = self.run_host({tile: {"mode": "everyone", "users": ["bob"], "groups": []}})["calls"][1]["rules"]
        self.assertIn({"entity_type": "user", "entity_name": "bob", "app_id": tile, "allow_ip": [], "deny_ip": ["0.0.0.0"]}, rules)
        self.assertIn({"entity_type": "group", "entity_name": "users", "app_id": tile, "allow_ip": ["0.0.0.0"], "deny_ip": []}, rules)

    def test_earlier_choices_are_removed(self):
        tile = core.TILE_PREFIX + "Immich"
        old = [{"entity_type": "user", "entity_name": "bob", "app_id": tile, "allow_ip": ["0.0.0.0"], "deny_ip": []},
               {"entity_type": "user", "entity_name": "anna", "app_id": tile, "allow_ip": ["0.0.0.0"], "deny_ip": []}]
        calls = self.run_host({tile: {"mode": "people", "users": ["anna"], "groups": []}}, old)["calls"]
        self.assertEqual([call["method"] for call in calls], ["list", "delete", "set"])
        self.assertEqual([rule["entity_name"] for rule in calls[1]["rules"]], ["bob"])
        calls = self.run_host({tile: {"mode": "none", "users": [], "groups": []}}, old)["calls"]
        self.assertEqual([call["method"] for call in calls], ["list", "delete"])
        self.assertEqual(len(calls[1]["rules"]), 2)


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

    def test_keys_used_in_scripts_exist(self):
        import re
        ui = ROOT / "src" / "package" / "ui"
        english = self.load_texts()["en"]
        used = set()
        for name in ("app.js", "open.js"):
            script = (ui / name).read_text(encoding="utf-8")
            used |= set(re.findall(r'\bt\("([\w.]+)"', script))
            used |= set(re.findall(r'"((?:open|people|apps|edit|status|error|diag)\.[\w]+)"', script))
        self.assertEqual(sorted(key for key in used if key not in english and not key.endswith(".")), [])


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

    def test_package_source_lists_the_new_build(self):
        import build_feed
        spk = build_spk.build()
        feed = build_feed.build(spk)
        catalog = json.loads((feed / "index.json").read_text())
        self.assertEqual((feed / "index.html").read_text(), (feed / "index.json").read_text())
        self.assertEqual(list(catalog), ["packages"])
        (item,) = catalog["packages"]
        self.assertEqual((item["package"], item["version"]), ("Cattport", build_spk.VERSION))
        self.assertEqual(item["link"], build_feed.SITE + "packages/" + spk.name)
        data = (feed / "packages" / spk.name).read_bytes()
        self.assertEqual((item["size"], item["md5"]), (len(data), __import__("hashlib").md5(data).hexdigest()))
        for url in item["thumbnail"] + item["snapshot"]:
            self.assertTrue((feed / url[len(build_feed.SITE):]).exists(), url)
        self.assertTrue(item["changelog"] and "**" not in item["changelog"])

    def test_icon_keeps_dsm_margin(self):
        box = build_spk.draw_icon(256).getbbox()
        self.assertGreater(box[0], 8)
        self.assertLess(box[2], 248)


if __name__ == "__main__":
    unittest.main()
