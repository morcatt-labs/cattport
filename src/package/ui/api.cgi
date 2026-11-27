#!/usr/bin/python3
"""The Cattport API: POST JSON with the SynoToken. Opening a tile needs a signed-in user; everything else an administrator."""
import grp
import json
import os
import pwd
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
import cattport_core as core

AUTHENTICATE = "/usr/syno/synoman/webman/authenticate.cgi"
MAX_BODY = 64 * 1024 * 1024
MAX_OPEN_BODY = 4096


def respond(status, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    out.write(("Status: %s\r\nContent-Type: application/json; charset=utf-8\r\n"
               "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
               "Content-Length: %d\r\n\r\n" % (status, len(data))).encode("ascii"))
    out.write(data)


def signed_in_user():
    """The DSM account behind this request's session and SynoToken, or None."""
    token = os.environ.get("HTTP_X_SYNO_TOKEN", "")
    if not token:
        return None
    env = dict(os.environ)
    env["QUERY_STRING"] = "SynoToken=" + urllib.parse.quote(token, safe="")
    try:
        lines = subprocess.run([AUTHENTICATE], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=10, check=False).stdout.decode("utf-8", "replace").strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    user = lines[-1].strip() if lines else ""
    return user or None


def groups_of(user):
    """Every group the account belongs to, its primary group included."""
    names = set()
    try:
        record = pwd.getpwnam(user)
        names.update(grp.getgrgid(gid).gr_name for gid in os.getgrouplist(user, record.pw_gid))
    except (KeyError, OSError):
        pass
    try:
        names.update(group.gr_name for group in grp.getgrall() if user in group.gr_mem)
    except OSError:
        pass
    return sorted(names)


def person():
    """None when nobody is signed in; otherwise who it is, their groups and whether they administer DSM."""
    user = signed_in_user()
    if not user:
        return None
    groups = groups_of(user)
    return {"user": user, "groups": groups, "admin": "administrators" in groups}


def same_origin():
    origin = os.environ.get("HTTP_ORIGIN")
    return not origin or urllib.parse.urlsplit(origin).netloc == os.environ.get("HTTP_HOST")


def main():
    if os.environ.get("REQUEST_METHOD") != "POST" or not same_origin():
        return respond("405 Method Not Allowed", {"success": False, "error": "bad_request"})
    if os.environ.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
        return respond("415 Unsupported Media Type", {"success": False, "error": "bad_request"})
    who = person()
    if who is None:
        return respond("403 Forbidden", {"success": False, "error": "session"})
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    # Someone who isn't an administrator can only ask to open a tile, which takes a few bytes.
    if not 0 < length <= (MAX_BODY if who["admin"] else MAX_OPEN_BODY):
        return respond("413 Payload Too Large", {"success": False, "error": "bad_request"})
    try:
        request = json.loads(sys.stdin.buffer.read(length))
        if not isinstance(request, dict):
            return respond("400 Bad Request", {"success": False, "error": "bad_request"})
        action = str(request.get("action"))
        if action == "open":
            data = core.open_link(core.Paths(), request, who)
        elif not who["admin"]:
            return respond("403 Forbidden", {"success": False, "error": "not_admin"})
        else:
            data = core.handle(core.Paths(), action, request)
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
