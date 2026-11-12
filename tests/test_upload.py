"""Images sent from the browser: saved into <pane folder>/.devbox/uploads on the session's host."""
import os
import subprocess
import time
import urllib.error
import urllib.request

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def upload(console, query: str, data: bytes):
    req = urllib.request.Request(console.url + "/api/upload" + query, method="POST", data=data,
                                 headers={"Cookie": console.cookie, "Content-Type": "image/png"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            import json
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_upload_local_into_session_folder(console):
    proj = console.work / "proj-a"
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    console.ok("POST", "/api/sessions", {"name": "img", "cwd": str(proj), "command": "cat"})
    wid = console.window("img")["id"]

    st, r = upload(console, f"?window={wid}", PNG)
    assert st == 200, r
    path = r["path"]
    assert path.startswith(str(proj / ".devbox" / "uploads") + "/") and path.endswith(".png")
    assert open(path, "rb").read() == PNG
    assert ".devbox/" in (proj / ".git/info/exclude").read_text().split("\n")
    assert subprocess.run(["git", "-C", str(proj), "status", "--porcelain"], capture_output=True,
                          text=True).stdout == "", "uploads stay out of git"

    # older than 7 days -> removed on the next upload; newer ones stay
    old = proj / ".devbox/uploads/old.png"
    old.write_bytes(PNG)
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    st, r2 = upload(console, f"?window={wid}", PNG)
    assert st == 200 and not old.exists() and os.path.exists(path)
    assert r2["path"] != path


def test_upload_rejects_non_images_and_bad_ids(console):
    assert upload(console, "", b"#!/bin/sh\necho hi\n")[0] == 400
    assert upload(console, "?window=@1;rm", PNG)[0] == 400
    assert upload(console, "?host=../x", PNG)[0] == 400
    req = urllib.request.Request(console.url + "/api/upload", method="POST", data=PNG)
    try:
        urllib.request.urlopen(req)
        assert False, "upload without login accepted"
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_upload_too_large(console):
    st, _ = upload(console, "", PNG + b"\0" * (10 * 1024 * 1024))
    assert st == 413


def test_upload_remote(console):
    console.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    (console.remote_home / "proj-r").mkdir()
    console.ok("POST", "/api/sessions?host=gpu-box", {"name": "rimg", "cwd": "~/proj-r", "command": "cat"})
    wid = console.window("rimg", host="gpu-box")["id"]
    st, r = upload(console, f"?host=gpu-box&window={wid}", PNG)
    assert st == 200, r
    assert r["path"].startswith(str(console.remote_home / "proj-r/.devbox/uploads/"))
    assert open(r["path"], "rb").read() == PNG
