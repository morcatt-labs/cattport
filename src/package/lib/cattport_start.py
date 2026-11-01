#!/usr/bin/python3
"""Rebuilds the tiles when the package starts. A data problem must not remove Cattport itself from DSM."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import cattport_core as core


def main():
    paths = core.Paths()
    try:
        with core._exclusive(paths):
            core.publish(paths, restarted=True)
    except (core.ApiError, OSError, ValueError, KeyError) as error:
        sys.stderr.write("cattport: tiles not rebuilt: %r\n" % (error,))
        # Tiles already published stay; only a missing config, as after an upgrade, is rebuilt.
        if not os.path.exists(paths.config):
            config = core.build_config({"apps": []}, core.version(paths))
            core._write_atomic(paths.config, json.dumps(config, indent=2).encode("utf-8"))


if __name__ == "__main__":
    main()
