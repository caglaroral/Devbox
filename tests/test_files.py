"""Files panel API: list / recent / read / write on the session's host (local and over SSH)."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def raw(console, method, path, data=None, headers=None):
    req = urllib.request.Request(console.url + path, method=method, data=data,
                                 headers={"Cookie": console.cookie, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def q(path, host=""):
    return f"path={urllib.parse.quote(str(path))}" + (f"&host={host}" if host else "")


def test_list_recent_read(console):
    d = console.work / "proj-a"
    (d / "sub").mkdir()
    (d / "chart.png").write_bytes(PNG)
    (d / "report.md").write_text("# Hi\n![x](chart.png)\n")
    (d / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    (d / "pic.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
    (d / ".hidden").write_text("x")
    old = d / "old.txt"
    old.write_text("old")
    os.utime(old, (time.time() - 3 * 86400,) * 2)

    r = console.ok("GET", f"/api/fs/list?{q(d)}")
    names = {e["name"]: e for e in r["entries"]}
    assert r["path"] == str(d)
    assert names["sub"]["type"] == "dir" and names["chart.png"]["size"] == len(PNG) and ".hidden" in names
    assert names["chart.png"]["mtime"] > time.time() - 120

    recent = [e["name"] for e in console.ok("GET", f"/api/fs/recent?{q(d)}&minutes=60")["entries"]]
    assert "chart.png" in recent and "old.txt" not in recent

    st, h, body = raw(console, "GET", f"/api/fs/read?{q(d / 'chart.png')}")
    assert st == 200 and body == PNG and h["Content-Type"] == "image/png"
    assert h["X-Version"] == f"{len(PNG)} {int((d / 'chart.png').stat().st_mtime)}"
    st, h, _ = raw(console, "GET", f"/api/fs/read?{q(d / 'doc.pdf')}")
    assert h["Content-Type"] == "application/pdf" and "Content-Security-Policy" not in h, "Chrome won't show a sandboxed PDF"
    st, h, _ = raw(console, "GET", f"/api/fs/read?{q(d / 'pic.svg')}")
    assert "sandbox" in h["Content-Security-Policy"], "an SVG must never run as a page of the console"
    st, h, body = raw(console, "GET", f"/api/fs/read?{q(d / 'report.md')}&download=1")
    assert h["Content-Type"].startswith("text/plain") and h["Content-Disposition"].startswith("attachment")

    assert raw(console, "GET", f"/api/fs/read?{q(d / 'nope.txt')}")[0] == 404
    assert raw(console, "GET", f"/api/fs/read?{q(d)}")[0] == 404, "a folder is not a file"
    assert console.call("GET", "/api/fs/list?path=relative/x")[0] == 400
    assert console.call("GET", f"/api/fs/list?{q(d / 'missing')}")[0] == 404


def test_home_paths(console):
    """claude prints ~/… paths: they resolve to the host user's home (here the fake remote's)."""
    console.ok("POST", "/api/hosts", {"label": "GPU Box", "hostname": "fakehost", "username": "dev", "password": "secret"})
    (console.remote_home / "out").mkdir()
    (console.remote_home / "out" / "a.txt").write_text("remote file")
    r = console.ok("GET", "/api/fs/list?path=~/out&host=gpu-box")
    assert r["path"] == str(console.remote_home / "out") and [e["name"] for e in r["entries"]] == ["a.txt"]
    st, _, body = raw(console, "GET", "/api/fs/read?path=~/out/a.txt&host=gpu-box")
    assert st == 200 and body == b"remote file"


def test_write_conflict_force_new(console):
    d = console.work / "proj-b"
    f = d / "app.py"
    f.write_text("print(1)\n")
    os.chmod(f, 0o755)
    st, h, _ = raw(console, "GET", f"/api/fs/read?{q(f)}")
    v = h["X-Version"]

    st, _, body = raw(console, "PUT", f"/api/fs/write?{q(f)}", b"print(2)\n", {"X-Version": v})
    assert st == 200 and f.read_text() == "print(2)\n"
    v2 = json.loads(body)["version"]
    assert os.stat(f).st_mode & 0o777 == 0o755, "saving keeps the file mode"

    # claude changed it meanwhile: saving with the old version is refused
    time.sleep(1.1)
    f.write_text("print('claude')\n")
    st, _, body = raw(console, "PUT", f"/api/fs/write?{q(f)}", b"print(3)\n", {"X-Version": v2})
    assert st == 409 and json.loads(body)["conflict"] and f.read_text() == "print('claude')\n"
    st, _, _ = raw(console, "PUT", f"/api/fs/write?{q(f)}&force=1", b"print(3)\n", {"X-Version": v2})
    assert st == 200 and f.read_text() == "print(3)\n"

    nf = d / "new" / "notes.md"
    st, _, _ = raw(console, "PUT", f"/api/fs/write?{q(nf)}", "ğüş\n".encode(), {"X-Version": ""})
    assert st == 200 and nf.read_text() == "ğüş\n"
    assert raw(console, "PUT", f"/api/fs/write?{q(nf)}", b"x", {"X-Version": ""})[0] == 409, "exists now"
    assert any(" file.save " in l for l in console.ok("GET", "/api/logs")["lines"])


def test_claude_gets_the_files_hint(console):
    import re
    console.ok("POST", "/api/sessions", {"name": "fh", "cwd": str(console.work / "proj-a"), "command": console.claude})
    text = console.capture("fh", needle="FAKECLAUDE")
    assert "--append-system-prompt" in text, "claude is told that printed paths open in the Files panel"
