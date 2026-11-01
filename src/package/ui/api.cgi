#!/usr/bin/python3
"""The Cattport API: signed-in DSM administrators only, POST JSON with the SynoToken."""
import grp
import json
import os
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
import cattport_core as core

AUTHENTICATE = "/usr/syno/synoman/webman/authenticate.cgi"
MAX_BODY = 64 * 1024 * 1024


def respond(status, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    out.write(("Status: %s\r\nContent-Type: application/json; charset=utf-8\r\n"
               "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
               "Content-Length: %d\r\n\r\n" % (status, len(data))).encode("ascii"))
    out.write(data)


def session_error():
    """None for a signed-in administrator; otherwise the code the page explains."""
    token = os.environ.get("HTTP_X_SYNO_TOKEN", "")
    if not token:
        return "session"
    env = dict(os.environ)
    env["QUERY_STRING"] = "SynoToken=" + urllib.parse.quote(token, safe="")
    try:
        user = subprocess.run([AUTHENTICATE], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10, check=False).stdout.decode("utf-8", "replace").strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return "session"
    user = user[-1].strip() if user else ""
    if not user:
        return "session"
    try:
        return None if user == "admin" or user in grp.getgrnam("administrators").gr_mem else "not_admin"
    except KeyError:
        return "not_admin"


def same_origin():
    origin = os.environ.get("HTTP_ORIGIN")
    return not origin or urllib.parse.urlsplit(origin).netloc == os.environ.get("HTTP_HOST")


def main():
    if os.environ.get("REQUEST_METHOD") != "POST" or not same_origin():
        return respond("405 Method Not Allowed", {"success": False, "error": "bad_request"})
    if os.environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
        return respond("415 Unsupported Media Type", {"success": False, "error": "bad_request"})
    denied = session_error()
    if denied:
        return respond("403 Forbidden", {"success": False, "error": denied})
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if not 0 < length <= MAX_BODY:
        return respond("413 Payload Too Large", {"success": False, "error": "bad_request"})
    try:
        request = json.loads(sys.stdin.buffer.read(length))
        data = core.handle(core.Paths(), str(request.get("action")), request)
        return respond("200 OK", {"success": True, "data": data})
    except core.ApiError as error:
        return respond("200 OK", {"success": False, "error": str(error)})
    except (AttributeError, TypeError) as error:
        sys.stderr.write("cattport: %r\n" % (error,))
        return respond("400 Bad Request", {"success": False, "error": "bad_request"})
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write("cattport: %r\n" % (error,))
        return respond("500 Internal Server Error", {"success": False, "error": "internal"})


if __name__ == "__main__":
    main()
