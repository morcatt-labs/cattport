#!/usr/bin/env python3
"""Builds the Morcatt Labs package source that DSM's Package Center reads: dist/feed/index.json and the files it points to.

The feed is static and served by GitHub Pages. It lists only the newest version, which DSM offers as an update.
Usage: python3 build_feed.py [path/to/Cattport-<version>.spk]   (defaults to the version in build_spk.py)
"""
import hashlib
import json
import shutil
import sys
import tarfile
from pathlib import Path

import build_spk

ROOT = Path(__file__).resolve().parent
FEED = ROOT / "dist" / "feed"
SITE = "https://morcatt-labs.github.io/cattport/"
CHANGELOG = ROOT / "CHANGELOG.md"


def spk_info(path):
    with tarfile.open(path) as spk:
        lines = spk.extractfile("INFO").read().decode("utf-8").splitlines()
    info = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            info[key.strip()] = value.strip().strip('"')
    return info


def latest_changes():
    """The bullet points of the newest section in CHANGELOG.md, as DSM shows them under "What's new"."""
    sections = CHANGELOG.read_text(encoding="utf-8").split("\n## ")[1:]
    if not sections:
        return ""
    bullets = [line[2:].replace("**", "").strip() for line in sections[0].splitlines() if line.startswith("- ")]
    return "<br>".join(bullets)


def entry(spk):
    info = spk_info(spk)
    data = spk.read_bytes()
    version = info["version"]
    icon = SITE + "images/cattport_256.png"
    return {
        "package": info["package"],
        "version": version,
        "dname": info["displayname"],
        "desc": info["description"],
        "price": 0,
        "download_count": 0,
        "recent_download_count": 0,
        # Served from the same site, so DSM never has to follow a redirect to download it.
        "link": SITE + "packages/" + spk.name,
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "thumbnail": [SITE + "images/cattport_72.png", icon],
        "thumbnail_retina": [icon],
        "snapshot": [SITE + "images/apps-light.png", SITE + "images/dsm-menu.png"],
        "qinst": True,
        "qstart": True,
        "qupgrade": True,
        "depsers": None,
        "deppkgs": None,
        "conflictpkgs": None,
        "start": True,
        "maintainer": info["maintainer"],
        "maintainer_url": info["maintainer_url"],
        "distributor": info["maintainer"],
        "distributor_url": info["maintainer_url"],
        "support_url": info["support_url"],
        "changelog": latest_changes(),
        "thirdparty": True,
        "category": 0,
        "subcategory": 0,
        "type": 0,
        "silent_install": False,
        "silent_uninstall": False,
        "silent_upgrade": False,
        "auto_upgrade_from": None,
        "os_min_ver": info["os_min_ver"],
        "firmware": info["os_min_ver"],
        "beta": False,
    }


def build(spk):
    if FEED.exists():
        shutil.rmtree(FEED)
    for folder in ("images", "packages"):
        (FEED / folder).mkdir(parents=True)
    for size in (72, 256):
        (FEED / "images" / ("cattport_%d.png" % size)).write_bytes(build_spk.png(build_spk.draw_icon(size)))
    for name in ("apps-light.png", "dsm-menu.png"):
        shutil.copyfile(ROOT / "docs" / "assets" / name, FEED / "images" / name)
    shutil.copyfile(spk, FEED / "packages" / spk.name)
    # DSM 7 reads only "packages"; keyrings and signatures were removed in DSM 7.0.
    body = json.dumps({"packages": [entry(spk)]}, ensure_ascii=False, indent=2) + "\n"
    # DSM asks for the address it was given, with its details in the query: the site's root answers with the
    # catalog itself, and index.json is the same file for anyone who prefers a file name.
    for name in ("index.html", "index.json"):
        (FEED / name).write_text(body, encoding="utf-8")
    (FEED / ".nojekyll").write_text("", encoding="utf-8")
    return FEED


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / ("Cattport-%s.spk" % build_spk.VERSION)
    sys.stdout.write("%s\n" % build(path))
