#!/usr/bin/env python3
"""Builds dist/Cattport-<version>.spk. Needs Pillow only to draw the icon."""
import io
import sys
import tarfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

VERSION = "1.0.0-0017"
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
ICON_SIZES = (16, 24, 32, 48, 64, 72, 256)
NOOP_SCRIPTS = ("preinst", "postinst", "preuninst", "preupgrade", "postupgrade")
NOOP = b"#!/bin/sh\nexit 0\n"
BLUE, WHITE, PALE = (10, 91, 211, 255), (255, 255, 255, 255), (191, 216, 250, 255)
INFO = {
    "package": "Cattport",
    "version": VERSION,
    "os_min_ver": "7.0-40000",
    "displayname": "Cattport",
    "description": "All your Docker services and links as tiles in the DSM menu, in one place. It never changes Docker or your services.",
    "description_rum": "Toate serviciile din Docker și linkurile tale ca dale în meniul DSM, într-un singur loc. Nu modifică Docker sau serviciile.",
    "arch": "noarch",
    "maintainer": "Morcatt Labs",
    "maintainer_url": "https://github.com/morcatt-labs/cattport",
    "support_url": "https://github.com/morcatt-labs/cattport/issues",
    "thirdparty": "yes",
    "dsmuidir": "ui",
    "dsmappname": "labs.morcatt.cattport.Instance",
    "install_dep_packages": "",
}


def draw_icon(size):
    """Icon A, "Cat tiles": four tiles, the top two with ears. Drawn large, then scaled down."""
    scale = 8
    unit = size * scale / 64
    image = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # The same transparent margin as DSM's own icons (about 88%); at 16 px it fills the canvas.
    edge = 0 if size < 24 else 4
    draw.rounded_rectangle((edge * unit, edge * unit, (64 - edge) * unit, (64 - edge) * unit), radius=14 * unit, fill=BLUE)
    for points in (((16, 30), (16, 15), (30, 22), (30, 30)), ((48, 30), (48, 15), (34, 22), (34, 30))):
        scaled = [(x * unit, y * unit) for x, y in points]
        draw.polygon(scaled, fill=WHITE)
        draw.line(scaled + scaled[:1], fill=WHITE, width=max(1, round(2.5 * unit)), joint="curve")
    draw.rounded_rectangle((15 * unit, 34 * unit, 31 * unit, 49 * unit), radius=3.5 * unit, fill=PALE)
    draw.rounded_rectangle((33 * unit, 34 * unit, 49 * unit, 49 * unit), radius=3.5 * unit, fill=WHITE)
    return image.resize((size, size), Image.LANCZOS)


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def add_bytes(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    # A real date, so browsers see that a file changed after an upgrade.
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def build_package_tgz():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted((SRC / "package").rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(SRC / "package").as_posix()
                data = path.read_bytes().replace(b"__VERSION__", VERSION.encode("ascii"))
                add_bytes(tar, relative, data, 0o755 if path.suffix == ".cgi" else 0o644)
        add_bytes(tar, "lib/version", VERSION.encode("ascii"))
        for size in ICON_SIZES:
            add_bytes(tar, "ui/images/cattport_%d.png" % size, png(draw_icon(size)))
    return buffer.getvalue()


def build():
    info = "".join('%s="%s"\n' % (key, value) for key, value in INFO.items()).encode("utf-8")
    out = ROOT / "dist" / ("Cattport-%s.spk" % VERSION)
    out.parent.mkdir(exist_ok=True)
    with tarfile.open(out, "w") as spk:
        add_bytes(spk, "INFO", info)
        add_bytes(spk, "package.tgz", build_package_tgz())
        for name in NOOP_SCRIPTS:
            add_bytes(spk, "scripts/" + name, NOOP, 0o755)
        for name in ("start-stop-status", "postuninst"):
            add_bytes(spk, "scripts/" + name, (SRC / "scripts" / name).read_bytes(), 0o755)
        add_bytes(spk, "conf/privilege", (SRC / "conf" / "privilege").read_bytes())
        for path in sorted((SRC / "WIZARD_UIFILES").iterdir()):
            add_bytes(spk, "WIZARD_UIFILES/" + path.name, path.read_bytes())
        add_bytes(spk, "LICENSE", (ROOT / "LICENSE").read_bytes())
        add_bytes(spk, "PACKAGE_ICON.PNG", png(draw_icon(64)))
        add_bytes(spk, "PACKAGE_ICON_256.PNG", png(draw_icon(256)))
    return out


if __name__ == "__main__":
    sys.stdout.write("%s\n" % build())
