"""Secrets in a real HashiCorp Vault (managed init/unseal, scoped token). Needs Docker; skipped otherwise."""
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT, free_port

IMAGE = "hashicorp/vault:1.18.3"


def docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0 or \
        subprocess.run(["docker", "pull", "-q", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ok(), reason=f"docker with {IMAGE} not available")


def vget(addr, path, token):
    req = urllib.request.Request(addr + path, headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


@pytest.fixture
def vault():
    port = free_port()
    name = f"devbox-vault-test-{port}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--cap-add", "IPC_LOCK",
                    "-v", f"{ROOT / 'src/vault/vault.hcl'}:/vault/config/vault.hcl:ro",
                    "-p", f"127.0.0.1:{port}:8200", IMAGE, "server"], check=True, capture_output=True)
    addr = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(addr + "/v1/sys/seal-status", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    yield addr, name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_secrets_live_in_vault(make_console, vault, tmp_path):
    addr, name = vault
    c = make_console({"VAULT_ADDR": addr}, base=tmp_path)
    s = c.ok("GET", "/api/settings")
    assert "vault" in json.dumps(s.get("secrets", s)).lower()

    c.ok("PUT", "/api/env", {"name": "ANTHROPIC_API_KEY", "value": "sk-vault-abcdef123456"})
    r = c.ok("POST", "/api/hosts", {"label": "Pw Box", "hostname": "fakehost", "username": "dev",
                                    "password": "secret", "store_password": True})
    assert {h["id"]: h for h in r["hosts"]}["pw-box"]["auth"] == "password"
    c.ok("POST", "/api/sessions?host=pw-box", {"name": "pw1", "cwd": "~",
                                               "command": "bash -c 'echo key=$ANTHROPIC_API_KEY; sleep 300'"})
    assert "key=sk-vault-abcdef123456" in c.capture("pw1", host="pw-box", needle="key=")

    init = json.loads((c.data / "vault-init.json").read_text())
    root = init["root_token"]
    for path in ("env", "ssh-key", "hosts/pw-box"):
        st, _ = vget(addr, f"/v1/devbox/data/users/{c.user}/{path}", root)
        assert st == 200, f"{path} not in vault"
    assert vget(addr, "/v1/sys/mounts", init["app_token"])[0] == 403, "app token is scoped to devbox/"

    on_disk = subprocess.run(["grep", "-rl", "sk-vault-abcdef123456", str(c.data)], capture_output=True, text=True).stdout
    assert not on_disk, f"plaintext secret on disk: {on_disk}"

    c.ok("DELETE", "/api/sessions/pw1?host=pw-box")
    c.ok("DELETE", "/api/hosts/pw-box")
    assert vget(addr, f"/v1/devbox/data/users/{c.user}/hosts/pw-box", root)[0] == 404

    # restart Vault (sealed again) and the console: it must unseal and still read the secrets
    c.stop()
    subprocess.run(["docker", "restart", name], check=True, capture_output=True)
    time.sleep(2)
    c2 = make_console({"VAULT_ADDR": addr}, base=tmp_path)
    env = c2.ok("GET", "/api/settings")
    assert "ANTHROPIC_API_KEY" in json.dumps(env)
