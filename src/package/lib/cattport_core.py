"""Cattport: state, icons and DSM tile generation. Standard library only (Python 3.8)."""
import base64
import concurrent.futures
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

PACKAGE = "Cattport"
NAMESPACE = "labs.morcatt.cattport"
TILE_PREFIX = NAMESPACE + ".tile."
SCHEMA = 1
ICON_SIZES = (16, 24, 32, 48, 64, 72, 256)
MAX_ICON_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_HTML_BYTES = 512 * 1024
MAX_APPS = 200
FETCH_TIMEOUT = 6
USER_AGENT = "Cattport (Synology DSM)"
ICON_CDNS = (
    ("Dashboard Icons", "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/{slug}.png"),
    ("selfh.st", "https://cdn.jsdelivr.net/gh/selfhst/icons/png/{slug}.png"),
    ("Dashboard Icons", "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{slug}.svg"),
    ("selfh.st", "https://cdn.jsdelivr.net/gh/selfhst/icons/svg/{slug}.svg"),
)
NAME_SUFFIXES = ("server", "web", "app", "docker", "ui", "main")
LANGUAGES = ("auto", "en", "ro", "cs", "da", "de", "es", "fr", "hu", "it", "ja", "ko", "nb", "nl", "pl", "pt-BR", "pt-PT", "ru", "sv", "th", "tr", "zh-Hans", "zh-Hant")
DEFAULT_SETTINGS = {"language": "auto", "theme": "auto", "online": None, "welcomed": False}


class ApiError(Exception):
    """The message is a code; the page translates it."""


class Paths:
    def __init__(self, target=None, var=None):
        self.target = target or os.environ.get("SYNOPKG_PKGDEST") or "/var/packages/%s/target" % PACKAGE
        self.var = var or os.environ.get("SYNOPKG_PKGVAR") or "/var/packages/%s/var" % PACKAGE
        self.state = os.path.join(self.var, "apps.json")
        self.backup = self.state + ".bak"
        self.icons = os.path.join(self.var, "icons")
        self.ui = os.path.join(self.target, "ui")
        self.config = os.path.join(self.ui, "config")
        self.images = os.path.join(self.ui, "images")
        self.version = os.path.join(self.target, "lib", "version")
        self.lock = os.path.join(self.var, ".lock")


def _write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_state(path):
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict) or not isinstance(state.get("apps"), list):
        raise ValueError("state shape")
    return state


def migrate(state):
    """Upgrades older formats in place; a newer format is refused so its data stays untouched."""
    version = state.get("schema", 1)
    if version > SCHEMA:
        raise ApiError("newer_data")
    state["schema"] = SCHEMA
    state["settings"] = dict(DEFAULT_SETTINGS, **state.get("settings", {}))
    return state


def load_state(paths):
    """Returns the state; a damaged file falls back to the last good copy and is reported once."""
    if not os.path.exists(paths.state):
        return migrate({"apps": []})
    try:
        return migrate(_read_state(paths.state))
    except (OSError, ValueError):
        pass
    try:
        state = migrate(_read_state(paths.backup))
    except (OSError, ValueError):
        raise ApiError("data_damaged")
    state["restored"] = True
    return state


def save_state(paths, state):
    if os.path.exists(paths.state):
        try:
            _read_state(paths.state)
            shutil.copyfile(paths.state, paths.backup)
        except (OSError, ValueError):
            pass
    _write_atomic(paths.state, json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"))


def make_key(name, taken):
    base = re.sub(r"[^A-Za-z0-9]", "", name)[:32] or "App"
    if base[0].isdigit():
        base = "App" + base
    key, counter = base, 2
    lowered = {item.lower() for item in taken}
    while key.lower() in lowered:
        key = "%s%d" % (base, counter)
        counter += 1
    return key


def validate_app(raw):
    if not isinstance(raw, dict):
        raise ApiError("invalid_app")
    name = str(raw.get("name", "")).strip()
    url = str(raw.get("url", "")).strip()
    if not 1 <= len(name) <= 48:
        raise ApiError("name_length")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ApiError("invalid_url")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError("invalid_url")
    external = str(raw.get("external") or "").strip()
    if external:
        try:
            outside = urllib.parse.urlsplit(external)
        except ValueError:
            raise ApiError("invalid_url")
        if outside.scheme not in ("http", "https") or not outside.hostname:
            raise ApiError("invalid_url")
    app = {
        "name": name,
        "url": url,
        "external": external,
        "desc": str(raw.get("desc", "")).strip()[:120],
        "allUsers": bool(raw.get("allUsers", False)),
        "hidden": bool(raw.get("hidden", False)),
        "follow": False,
    }
    # A Docker tile with no path follows whichever address DSM was opened on.
    if raw.get("follow") and port and parsed.path in ("", "/") and not parsed.query:
        app["follow"] = True
    return app


def _png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ApiError("icon_not_png")
    return struct.unpack(">II", data[16:24])


def decode_icons(icons):
    if not isinstance(icons, dict) or set(icons) != {str(size) for size in ICON_SIZES}:
        raise ApiError("icon_incomplete")
    decoded = {}
    for size, payload in icons.items():
        try:
            data = base64.b64decode(str(payload), validate=True)
        except ValueError:
            raise ApiError("icon_invalid")
        if len(data) > MAX_ICON_BYTES:
            raise ApiError("icon_too_large")
        if _png_size(data) != (int(size), int(size)):
            raise ApiError("icon_invalid")
        decoded[size] = data
    return decoded


def _store_icons(paths, app, icons):
    for size, data in icons.items():
        _write_atomic(os.path.join(paths.icons, app["key"], "%s.png" % size), data)
    app["icon_rev"] = hashlib.sha256(b"".join(icons[str(s)] for s in ICON_SIZES)).hexdigest()[:12]


def _icon_bytes(paths, app, size):
    """A missing icon falls back to Cattport's own, so one bad app can't stop every other tile."""
    try:
        with open(os.path.join(paths.icons, app["key"], "%d.png" % size), "rb") as handle:
            return handle.read()
    except OSError:
        with open(os.path.join(paths.images, "cattport_%d.png" % size), "rb") as handle:
            return handle.read()


def _find(state, key):
    for app in state["apps"]:
        if app["key"] == key:
            return app
    raise ApiError("app_gone")


def save_app(paths, payload):
    state = load_state(paths)
    fields = validate_app(payload.get("app"))
    key = payload.get("key")
    icons = decode_icons(payload["icons"]) if payload.get("icons") else None
    if key:
        app = _find(state, key)
    else:
        if len(state["apps"]) >= MAX_APPS:
            raise ApiError("too_many_apps")
        if icons is None:
            raise ApiError("icon_required")
        app = {"key": make_key(fields["name"], [item["key"] for item in state["apps"]])}
        state["apps"].append(app)
    app.update(fields)
    if icons:
        _store_icons(paths, app, icons)
    save_state(paths, state)
    return public_state(paths, state)


def _trash_path(paths):
    return os.path.join(paths.var, "trash")


def delete_app(paths, payload):
    """Moves the app to the trash, so Undo can bring it back even after it was published."""
    state = load_state(paths)
    app = _find(state, payload.get("key"))
    state["apps"].remove(app)
    trash = _trash_path(paths)
    shutil.rmtree(trash, ignore_errors=True)
    os.makedirs(trash)
    # The key matched the saved state exactly, so it cannot leave the icons directory.
    source = os.path.join(paths.icons, app["key"])
    if os.path.isdir(source):
        shutil.move(source, os.path.join(trash, "icons"))
    state["trash"] = app
    save_state(paths, state)
    return public_state(paths, state)


def undo_delete(paths):
    state = load_state(paths)
    app = state.pop("trash", None)
    if app is None:
        raise ApiError("app_gone")
    icons = os.path.join(_trash_path(paths), "icons")
    if os.path.isdir(icons):
        os.makedirs(paths.icons, exist_ok=True)
        shutil.move(icons, os.path.join(paths.icons, app["key"]))
    state["apps"].append(app)
    save_state(paths, state)
    return public_state(paths, state)


def save_settings(paths, payload):
    state = load_state(paths)
    raw = payload.get("settings") or {}
    settings = state["settings"]
    if raw.get("language") in LANGUAGES:
        settings["language"] = raw["language"]
    if raw.get("theme") in ("auto", "light", "dark"):
        settings["theme"] = raw["theme"]
    if isinstance(raw.get("online"), bool):
        settings["online"] = raw["online"]
    if raw.get("welcomed") is True:
        settings["welcomed"] = True
    save_state(paths, state)
    return public_state(paths, state)


def version(paths):
    try:
        with open(paths.version, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return "dev"


def tile_entry(app):
    entry = {
        "type": "url",
        "icon": "images/%s.%s_{0}.png" % (app["key"], app.get("icon_rev", "base")),
        "title": app["name"],
        "desc": app.get("desc", ""),
        "url": app["url"],
        "allUsers": bool(app.get("allUsers")),
    }
    # No grantPrivilege: DSM would then need a privilege record, which a published tile doesn't have, and hide it from everyone.
    if app.get("external"):
        entry["url"] = "webman/3rdparty/%s/open.html?app=%s" % (PACKAGE, app["key"])
    elif app.get("follow"):
        parsed = urllib.parse.urlsplit(app["url"])
        entry.update(url="/", port=parsed.port, protocol=parsed.scheme)
    return entry


def build_links(state):
    """What open.html needs for tiles with an outside address."""
    links = {}
    for app in state["apps"]:
        if app.get("external") and not app.get("hidden"):
            link = {"url": app["url"], "external": app["external"]}
            if app.get("follow"):
                parsed = urllib.parse.urlsplit(app["url"])
                link.update(port=parsed.port, protocol=parsed.scheme)
            links[app["key"]] = link
    return links


def build_config(state, js_version):
    icon = "images/cattport_{0}.png"
    app_class = NAMESPACE + ".Instance"
    window_class = NAMESPACE + ".AppWindow"
    # DSM loads cattport.js with ?v=<version>; without it browsers keep the old code after an upgrade.
    config = {
        "cattport.js": {
            app_class: {
                "type": "app",
                "version": js_version,
                "title": "Cattport",
                "desc": "All your apps, in one place",
                "icon": icon,
                "allUsers": False,
                "allowMultiInstance": False,
                "appWindow": window_class,
                "depend": [window_class],
            },
            window_class: {"type": "lib", "version": js_version, "title": "Cattport", "icon": icon},
        }
    }
    tiles = {TILE_PREFIX + app["key"]: tile_entry(app) for app in state["apps"] if not app.get("hidden")}
    if tiles:
        config[".url"] = tiles
    return config


def _tile_snapshot(state):
    """One fingerprint per visible tile, including where it opens; hidden apps have no tile."""
    links = build_links(state)
    snapshot = {}
    for app in state["apps"]:
        if not app.get("hidden"):
            content = json.dumps([tile_entry(app), links.get(app["key"])], sort_keys=True)
            snapshot[app["key"]] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return snapshot


def _pending_changes(state):
    """How many tiles differ from what DSM last loaded, counting added, changed and removed ones."""
    saved = state.get("deployed", {})
    current = _tile_snapshot(state)
    return sum(1 for key in set(saved) | set(current) if saved.get(key) != current.get(key))


def publish(paths, restarted=False):
    """Writes ui/config and the icons. The name carries the revision, so browsers never show a stale icon."""
    state = load_state(paths)
    os.makedirs(paths.images, exist_ok=True)
    wanted = set()
    for app in state["apps"]:
        if app.get("hidden"):
            continue
        for size in ICON_SIZES:
            name = "%s.%s_%d.png" % (app["key"], app.get("icon_rev", "base"), size)
            wanted.add(name)
            _write_atomic(os.path.join(paths.images, name), _icon_bytes(paths, app, size))
    config = build_config(state, version(paths))
    _write_atomic(os.path.join(paths.ui, "links.json"), json.dumps(build_links(state), ensure_ascii=False).encode("utf-8"))
    _write_atomic(paths.config, json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8"))
    for name in os.listdir(paths.images):
        if name not in wanted and not name.startswith("cattport_"):
            os.unlink(os.path.join(paths.images, name))
    state.pop("restored", None)
    # DSM refreshes its list only when the package stops, so only a restart counts as saved.
    if restarted:
        state["deployed"] = _tile_snapshot(state)
    save_state(paths, state)
    return {"ids": sorted(config.get(".url", {})), "state": public_state(paths, state)}


def public_state(paths, state=None):
    state = state or load_state(paths)
    apps = [dict(app, id=TILE_PREFIX + app["key"]) for app in state["apps"]]
    return {
        "apps": apps,
        "pending": _pending_changes(state),
        "settings": state["settings"],
        "canUndo": "trash" in state,
        "restored": bool(state.get("restored")),
        "version": version(paths),
        "lanIp": lan_ip(),
    }


def icon_preview(paths, payload):
    app = _find(load_state(paths), payload.get("key"))
    return {"dataUrl": "data:image/png;base64," + base64.b64encode(_icon_bytes(paths, app, 256)).decode("ascii")}


def export_data(paths):
    state = load_state(paths)
    apps = []
    for app in state["apps"]:
        icons = {}
        for size in ICON_SIZES:
            icons[str(size)] = base64.b64encode(_icon_bytes(paths, app, size)).decode("ascii")
        apps.append({"app": {name: app.get(name) for name in ("name", "url", "external", "desc", "allUsers", "hidden", "follow")}, "icons": icons})
    return {"format": "cattport-backup", "schema": SCHEMA, "apps": apps}


def import_data(paths, payload):
    """Validates everything first, so a bad file changes nothing. Existing apps with the same name stay."""
    backup = payload.get("backup")
    if not isinstance(backup, dict) or backup.get("format") != "cattport-backup" or not isinstance(backup.get("apps"), list):
        raise ApiError("invalid_backup")
    if backup.get("schema", 1) > SCHEMA:
        raise ApiError("newer_data")
    if len(backup["apps"]) > MAX_APPS:
        raise ApiError("too_many_apps")
    items = [(validate_app(item.get("app")), decode_icons(item.get("icons"))) for item in backup["apps"]]
    state = load_state(paths)
    names = {app["name"].lower() for app in state["apps"]}
    for fields, icons in items:
        if fields["name"].lower() in names:
            continue
        if len(state["apps"]) >= MAX_APPS:
            raise ApiError("too_many_apps")
        app = dict(fields, key=make_key(fields["name"], [item["key"] for item in state["apps"]]))
        _store_icons(paths, app, icons)
        state["apps"].append(app)
        names.add(fields["name"].lower())
    save_state(paths, state)
    return public_state(paths, state)


def lan_ip():
    """The NAS's own address; connect on UDP sends no packets."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def _is_local_host(host):
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


class _HttpOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """urllib also follows redirects to ftp://; a page must not be able to send us off the web."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme not in ("http", "https"):
            raise ApiError("invalid_url")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, limit):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ApiError("invalid_url")
    context = None
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        if _is_local_host(parsed.hostname):
            # LAN services usually have their own certificates; only an image is read, and it is validated below.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), _HttpOnlyRedirect)
    with opener.open(request, timeout=FETCH_TIMEOUT) as response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ApiError("image_too_large")
        return body, response.geturl()


def _sniff_image(body):
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if body[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    head = body[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if (head.startswith(b"<svg") or head.startswith(b"<?xml")) and b"<svg" in head:
        return "image/svg+xml"
    return None


def check_address(payload):
    """Any answer counts, even an error page: the warning only means nothing is listening."""
    try:
        _open(str(payload.get("url", "")), 1)
    except urllib.error.HTTPError:
        return {"reachable": True}
    except ApiError as error:
        return {"reachable": str(error) == "image_too_large"}
    except (OSError, ValueError):
        return {"reachable": False}
    return {"reachable": True}


def fetch_image(url):
    """Returned as a data URL and only ever shown through <img>, so an SVG cannot run scripts."""
    try:
        body, _ = _open(url, MAX_IMAGE_BYTES)
    except (OSError, ValueError):
        raise ApiError("image_unreachable")
    kind = _sniff_image(body)
    if kind is None:
        raise ApiError("not_an_image")
    return "data:%s;base64,%s" % (kind, base64.b64encode(body).decode("ascii"))


class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        values = dict(attrs)
        rel = (values.get("rel") or "").lower()
        href = values.get("href")
        if href and ("icon" in rel.split() or "apple-touch-icon" in rel):
            self.found.append((0 if "apple-touch-icon" in rel else 1, href))


def page_icon_urls(page_url):
    """The icons a page declares, then /favicon.ico."""
    candidates = []
    base = page_url
    try:
        body, base = _open(page_url, MAX_HTML_BYTES)
        parser = _IconLinks()
        parser.feed(body.decode("utf-8", "replace"))
        candidates = [urllib.parse.urljoin(base, href) for _, href in sorted(parser.found, key=lambda x: x[0])]
    except (ApiError, OSError, ValueError):
        pass
    candidates.append(urllib.parse.urljoin(base, "/favicon.ico"))
    return list(dict.fromkeys(candidates))


def _try_image(source, label, url):
    try:
        return {"source": source, "label": label, "dataUrl": fetch_image(url)}
    except (ApiError, OSError, ValueError):
        return None


def _first_page_icon(page_url, label, source):
    for url in page_icon_urls(page_url)[:6]:
        result = _try_image(source, label, url)
        if result:
            return result
    return None


def name_slugs(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slugs = [slug] if slug else []
    parts = slug.split("-")
    while len(parts) > 1 and parts[-1] in NAME_SUFFIXES:
        parts = parts[:-1]
        slugs.append("-".join(parts))
    if slug:
        slugs.append(slug.replace("-", ""))
    return list(dict.fromkeys(slugs))[:3]


def _run(tasks):
    if not tasks:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(func, *args) for func, args in tasks]
        seen, unique = set(), []
        for future in futures:
            item = future.result()
            if item and item["dataUrl"] not in seen:
                seen.add(item["dataUrl"])
                unique.append(item)
        return unique


def docker_icons(payload):
    """Icons from a container: image labels, then the web page behind its published ports."""
    tasks = []
    for label, url in list((payload.get("labels") or {}).items())[:8]:
        if str(url).startswith(("http://", "https://")):
            tasks.append((_try_image, ("docker", label, str(url))))
    for port in list(payload.get("ports") or [])[:6]:
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ApiError("invalid_port")
        for scheme in ("http", "https"):
            tasks.append((_first_page_icon, ("%s://127.0.0.1:%d/" % (scheme, port), "Port %d" % port, "docker")))
    return {"results": _run(tasks)}


def internet_icons(paths, payload):
    """Only runs after the administrator allowed online search."""
    if load_state(paths)["settings"].get("online") is not True:
        return {"results": [], "disabled": True}
    tasks = []
    for slug in name_slugs(str(payload.get("name", ""))):
        for label, template in ICON_CDNS:
            tasks.append((_try_image, ("internet", label, template.format(slug=slug))))
    site = str(payload.get("url", "")).strip()
    if site.startswith(("http://", "https://")):
        tasks.append((_first_page_icon, (site, "Website", "internet")))
    return {"results": _run(tasks)}


CHANGES_DATA = {"save_app", "delete_app", "undo_delete", "save_settings", "publish", "import"}


@contextlib.contextmanager
def _exclusive(paths):
    """Each API call is its own process; without this, two admins could overwrite each other's change."""
    os.makedirs(paths.var, exist_ok=True)
    with open(paths.lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def handle(paths, action, payload):
    if action in CHANGES_DATA:
        with _exclusive(paths):
            return _dispatch(paths, action, payload)
    return _dispatch(paths, action, payload)


def _dispatch(paths, action, payload):
    if action == "state":
        return public_state(paths)
    if action == "save_app":
        return save_app(paths, payload)
    if action == "delete_app":
        return delete_app(paths, payload)
    if action == "undo_delete":
        return undo_delete(paths)
    if action == "save_settings":
        return save_settings(paths, payload)
    if action == "publish":
        return publish(paths)
    if action == "icon":
        return icon_preview(paths, payload)
    if action == "export":
        return export_data(paths)
    if action == "import":
        return import_data(paths, payload)
    if action == "docker_icons":
        return docker_icons(payload)
    if action == "internet_icons":
        return internet_icons(paths, payload)
    if action == "check_address":
        return check_address(payload)
    if action == "fetch_image":
        return {"dataUrl": fetch_image(str(payload.get("url", "")))}
    raise ApiError("unknown_action")
