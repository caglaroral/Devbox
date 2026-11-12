"""Secret storage: HashiCorp Vault (KV v2) or 0600 files.

What is a secret here: API keys/env values, the per-user console SSH private key, pasted per-host
private keys and (optional) stored host passwords. Everything else stays in plain JSON under DATA.

Vault mode (VAULT_ADDR set):
  * VAULT_TOKEN given  -> use it as is (existing Vault, you manage it).
  * no VAULT_TOKEN     -> "managed" mode for the bundled compose Vault: on first start the console
                          initialises Vault (1 key share), stores the unseal key + root token in
                          DATA/vault-init.json (0600), unseals it on every start, mounts KV v2 at
                          VAULT_MOUNT and creates a token limited to that mount.
Paths: <mount>/data/users/<user>/<name>
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import aiohttp


class SecretStoreError(RuntimeError):
    pass


class FileStore:
    kind = "file"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _f(self, path: str) -> Path:
        safe = path.strip("/").replace("..", "_")
        return self.root / (safe + ".json")

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get(self, path: str) -> dict | None:
        try:
            return json.loads(self._f(path).read_text())
        except (OSError, ValueError):
            return None

    async def put(self, path: str, data: dict) -> None:
        f = self._f(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(f.parent, 0o700)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        tmp.replace(f)

    async def delete(self, path: str) -> None:
        self._f(path).unlink(missing_ok=True)

    def status(self) -> dict:
        return {"kind": "file", "location": str(self.root)}


class VaultStore:
    kind = "vault"

    def __init__(self, addr: str, token: str | None, mount: str, init_file: Path) -> None:
        self.addr = addr.rstrip("/")
        self.token = token
        self.managed = not token
        self.mount = mount.strip("/")
        self.init_file = init_file
        self.session: aiohttp.ClientSession | None = None
        self.ready = False
        self.last_error: str | None = None

    async def _req(self, method: str, path: str, body=None, token: str | None = None, ok404=False):
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        headers = {}
        tok = token if token is not None else self.token
        if tok:
            headers["X-Vault-Token"] = tok
        try:
            async with self.session.request(method, f"{self.addr}/v1/{path}", json=body, headers=headers) as r:
                if r.status == 404 and ok404:
                    return None
                text = await r.text()
                if r.status >= 400:
                    raise SecretStoreError(f"vault {method} {path}: {r.status} {text[:200]}")
                return json.loads(text) if text else {}
        except aiohttp.ClientError as e:
            raise SecretStoreError(f"vault erişilemiyor ({self.addr}): {e}") from e

    async def start(self) -> None:
        """Wait for Vault; in managed mode init/unseal/configure it."""
        import asyncio

        last = None
        for _ in range(60):
            try:
                health = await self._req("GET", "sys/seal-status", token="")
                break
            except SecretStoreError as e:
                last = e
                await asyncio.sleep(1)
        else:
            raise SecretStoreError(str(last))
        if self.managed:
            await self._managed_setup(health)
        else:
            await self._req("GET", "auth/token/lookup-self")
        self.ready = True

    async def _managed_setup(self, health: dict) -> None:
        init = None
        if self.init_file.exists():
            init = json.loads(self.init_file.read_text())
        if not health.get("initialized"):
            r = await self._req("PUT", "sys/init", {"secret_shares": 1, "secret_threshold": 1}, token="")
            # this response is the only copy of the unseal key: persist it before anything else can fail
            fd = os.open(self.init_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(r, f)
            init = {"keys_b64": r.get("keys_base64") or r.get("keys"), "root_token": r["root_token"]}
            self.init_file.write_text(json.dumps(init))
            health = await self._req("GET", "sys/seal-status", token="")
        if init is not None and "keys_b64" not in init:  # raw response saved by a crashed first start
            init = {"keys_b64": init.get("keys_base64") or init.get("keys"), "root_token": init["root_token"]}
        if init is None:
            raise SecretStoreError(f"Vault zaten init edilmiş ama {self.init_file} yok: VAULT_TOKEN verin")
        if health.get("sealed"):
            await self._req("PUT", "sys/unseal", {"key": init["keys_b64"][0]}, token="")
        root = init["root_token"]
        mounts = await self._req("GET", "sys/mounts", token=root)
        if f"{self.mount}/" not in mounts.get("data", mounts):
            await self._req("POST", f"sys/mounts/{self.mount}", {"type": "kv", "options": {"version": "2"}}, token=root)
        if not init.get("app_token"):
            policy = (f'path "{self.mount}/data/*" {{ capabilities = ["create","read","update","delete"] }}\n'
                      f'path "{self.mount}/metadata/*" {{ capabilities = ["read","delete","list"] }}\n')
            await self._req("PUT", "sys/policies/acl/devbox", {"policy": policy}, token=root)
            r = await self._req("POST", "auth/token/create",
                                {"policies": ["devbox"], "period": "768h", "no_parent": True,
                                 "display_name": "devbox-console"}, token=root)
            init["app_token"] = r["auth"]["client_token"]
            self.init_file.write_text(json.dumps(init))
            os.chmod(self.init_file, 0o600)
        self.token = init["app_token"]
        try:  # periodic token: renew on every start
            await self._req("POST", "auth/token/renew-self", {})
        except SecretStoreError:
            pass

    async def get(self, path: str) -> dict | None:
        r = await self._req("GET", f"{self.mount}/data/{path.strip('/')}", ok404=True)
        return None if r is None else (r.get("data") or {}).get("data")

    async def put(self, path: str, data: dict) -> None:
        await self._req("POST", f"{self.mount}/data/{path.strip('/')}", {"data": data})

    async def delete(self, path: str) -> None:
        await self._req("DELETE", f"{self.mount}/metadata/{path.strip('/')}", ok404=True)

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    def status(self) -> dict:
        return {"kind": "vault", "location": self.addr, "mount": self.mount,
                "mode": "managed" if self.managed else "external", "ready": self.ready}


def make_store(data_dir: Path):
    addr = os.environ.get("VAULT_ADDR", "").strip()
    if addr:
        return VaultStore(addr, os.environ.get("VAULT_TOKEN") or None,
                          os.environ.get("VAULT_MOUNT", "devbox"), data_dir / "vault-init.json")
    return FileStore(data_dir / "secrets")
