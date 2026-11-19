# Devbox agent: reaching hosts without a VPN (design)

Status: proposal, not implemented. Written for the "Devbox as a hosted service" question: the console runs in
the cloud and has to reach machines behind NAT, home routers and company firewalls.

## 1. Problem

Today the console **dials out** to every host over SSH (`Box.wrap()` → `ssh …`). That only works when the
console can reach the host's port 22: same LAN, a public IP, or a VPN (WireGuard today). For a hosted console
that means every customer must set up a VPN, on the host **and** on every device they open the console from,
with routes, DNS and IP ranges that clash with other VPNs, and one profile per device (the cause of the
disconnects we saw).

## 2. Goals / non-goals

Goals
- A host behind NAT / a firewall is usable from a cloud console with **only outbound HTTPS (443)** from the host.
- Nothing to install on the devices the console is opened from (browser only).
- One-line install, runs as the normal user (no root, no network changes).
- The customer stays in control: one command or one click disconnects it, and access is visible.
- **Reuse the console as it is**: tmux, terminals, files, uploads, "ask"-style locking keep working unchanged.
- **Open source** (agent, install script, protocol). It runs on the customer's machine with their user's rights,
  so anyone installing it must be able to read exactly what it does. It stays small on purpose (target: a few
  thousand lines of Go) so that a review is realistic.
- **Every major OS**: Linux, macOS and Windows (native, not only WSL), amd64 and arm64.

Non-goals
- General network access to the customer's network (that is what a VPN is for).
- Replacing plain SSH: hosts the console can already reach (a cloud VM with a public IP) keep using SSH directly.

## 3. Overview: SSH inside an outbound WebSocket

```
 customer machine (behind NAT)                       cloud
 ┌──────────────────────────────┐                   ┌──────────────────────────────────────────┐
 │ devbox-agent (user process)  │  wss://…/agent    │ console (aiohttp)                         │
 │  ├ control WebSocket ────────┼──────────────────►│  agent hub: who is online, open streams   │
 │  ├ data WebSocket per stream ┼──────────────────►│      ▲                                    │
 │  └ embedded SSH server       │                   │      │ unix socket                        │
 │     (only the console key,   │                   │  devbox-proxy  ◄── ssh ProxyCommand ◄──   │
 │      runs as this user)      │                   │                    Box.wrap() (unchanged) │
 │  tmux, claude, shell         │                   └──────────────────────────────────────────┘
 └──────────────────────────────┘                          ▲ HTTPS
                                                           │
                                                  browser (laptop / phone)
```

The agent is a small static binary that **is an SSH server, but listens on no port**. Instead it keeps one
outbound WebSocket to the console (control channel). When the console needs the host, `ssh` runs exactly as
today but with `ProxyCommand=devbox-proxy <agent-id>`; the proxy asks the hub for a stream, the hub tells the
agent over the control channel, the agent opens a second WebSocket for that stream and plugs it into its SSH
server. From `ssh`'s point of view it is an ordinary connection.

### Why SSH inside, not a custom protocol

Every remote action in the console already goes through an `ssh` subprocess: `Box.run()` (commands, tmux
polling, files), `term_buffer` (`tmux -C` control mode), `ws_term` (pty via `ssh -tt`), "ask" master
connections. Carrying SSH means we get for free, and keep tested:
- channels, pty + window size, exit codes, stdin/stdout/stderr separation,
- flow control (no unbounded buffers when a terminal floods),
- ControlMaster connection sharing,
- end-to-end auth and encryption **between the console and the agent**, independent of TLS termination
  (a proxy / load balancer in the middle sees only SSH bytes).

A custom JSON/binary exec protocol would mean re-implementing all of that and changing every call site.

### Compared with the alternatives

| | Install on host | Install on browsing devices | Network changes | Scope of access | Console changes |
|---|---|---|---|---|---|
| VPN (WireGuard/Tailscale) | client, often root | **yes**, each device | routes, DNS, IP range | whole network | none |
| `ssh -R` to a bastion | autossh + sshd on host | no | none | one port | port allocation, bastion hardening |
| **Agent (this design)** | one binary, user-level | no | none | this user's processes | `ProxyCommand` + hub |

`ssh -R` to a bastion is a fine **prototype** (see Phases), but needs sshd on the host, a shared bastion with
per-tenant port isolation (`PermitListen`), and it breaks on networks that only allow HTTPS through a proxy.

## 4. Components

### 4.1 devbox-agent (customer machine)
- Go, single static binary, no runtime dependencies: linux, darwin, windows × amd64, arm64 (and freebsd for
  free, Go cross-compiles). `golang.org/x/crypto/ssh` for the server side, `nhooyr.io/websocket` (or similar)
  for WSS; few, well-known dependencies.
- Pty per OS: `creack/pty` on Linux/macOS, **ConPTY** on Windows (Windows 10 1809+). On Windows commands run in
  the user's default shell (PowerShell) or in WSL when the user picks it; tmux itself needs a Unix side, so a
  native Windows host gets shells and files, and Claude sessions in tmux through WSL or a tmux alternative
  (open question).
- Runs as the **logged-in user**. `~/.config/devbox-agent/` holds its state (0600): agent id, its own
  host key, its enrollment credential, the console's allowed public key(s).
- SSH server features: `exec`, `shell`, `pty-req`, `window-change`, `env` (allow-list: `TERM`, `LANG`,
  `DEVBOX_*`). No port forwarding, no agent forwarding, no SFTP subsystem (the console doesn't use it).
  Commands run with the user's login shell, like sshd does, so `PATH`, `~/.local/bin/claude` etc. resolve the same.
- Supervised by `systemd --user` (Linux, with `loginctl enable-linger` so it survives logout), `launchd`
  (macOS, LaunchAgent) or a per-user Scheduled Task / Startup entry (Windows; no admin rights, no service
  install needed). Reconnects with exponential backoff (1 s → 60 s, jitter); honours `HTTPS_PROXY`.
- `devbox-agent status | pause | resume | uninstall`. Pause closes the control channel immediately.

### 4.2 Agent hub (inside the console)
- `GET /agent/connect` (WebSocket): agent control channel, authenticated (see 6.1). Keeps
  `agents[agent_id] = control_ws`, last seen, version, OS.
- `GET /agent/stream/{stream_id}` (WebSocket): data channel the agent opens on request; single-use stream token.
- Unix socket `RUNTIME/agent-hub.sock` for `devbox-proxy` (local only; checks the caller's console user).
- Broadcasts online / offline to the UI like host reachability today.

### 4.3 devbox-proxy (inside the console container)
- Tiny program used as `ProxyCommand`. Connects to the hub socket, asks for `agent_id`, then copies bytes
  between its stdin/stdout and the stream. Exits non-zero with a clear message when the agent is offline
  ("agent offline since 14:02"), which ends up in `host.error` like an SSH failure today.

## 5. Flows

### 5.1 Enrollment (adding a host)
1. Hosts page → **Add host → "Agent (behind NAT / firewall)"**. The console creates a pending host with an
   **enrollment token** (random, one-time, 15 min) and shows:
   ```
   curl -fsSL https://<console>/agent/install.sh | sh -s -- --token dbx_enr_…
   ```
2. The script downloads the right binary (checksum + signature verified), writes the user service, starts it.
   Windows: the same as a PowerShell one-liner (`irm https://<console>/agent/install.ps1 | iex`), plus
   `winget` / Homebrew / `.deb` / `.rpm` packages for people who prefer a package manager. The scripts are in the
   open source repo too, and the docs show how to install by hand (download, verify, run) without piping to a shell.
3. The agent generates its SSH host key and an enrollment key pair, connects to `/agent/connect` with the
   token, sends its public keys and basic facts (hostname, OS, user). The console:
   - stores the agent's **host key** in `known_hosts` under the agent id (TOFU at enrollment, pinned after),
   - stores the agent's **credential public key** (for later reconnects),
   - replies with the **console user's SSH public key**, the only key the agent's SSH server accepts,
   - burns the token.
4. The pending host turns into a normal host (`kind: "agent"`) and is probed (`_probe`) like today.

### 5.2 Reconnect (every start, every network change)
Agent → `/agent/connect` → server sends a nonce → agent signs it with its credential key → accepted if the key
is enrolled and not revoked. No long-lived bearer secret on disk that can simply be copied and replayed elsewhere
without the private key file.

### 5.3 A command / terminal
`Box.wrap()` builds `ssh … -o ProxyCommand="devbox-proxy %n" <agent-id>` → proxy asks the hub → hub sends
`{"t":"open","stream":"s_…","token":"…"}` on the control channel → agent dials `/agent/stream/s_…` → bytes flow
→ the agent's SSH server authenticates the console key and runs `tmux …`. Closing either side closes the stream.
ControlMaster keeps one stream per host for the many small `tmux` calls, as today.

### 5.4 Revocation / kill switch
- Customer side: `devbox-agent pause` or `uninstall`, or stop the service: effective immediately.
- Console side: **Delete host** revokes the credential key; the hub closes the control channel and refuses it.
- Account deletion revokes all agents of the account.

## 6. Security

### 6.1 Layers
1. **TLS** to the console (public certificate).
2. **Agent identity**: challenge signed with the enrolled credential key (5.2). Revocable per agent.
3. **SSH between console and agent**: the agent only accepts the console user's key; the console pins the
   agent's host key. A compromised load balancer or hub process without the console user's private key still
   cannot run commands.
4. **Stream tokens**: single-use, bound to agent id + stream id, short expiry.

### 6.2 Open source and verifiable builds
- Public repository (license: Apache-2.0 or MIT) containing the agent, install scripts and the protocol spec.
- **Reproducible builds**: the release binaries can be rebuilt from the tagged source byte for byte
  (`-trimpath`, pinned Go version, no build-time data), and CI publishes the checksums; anyone can confirm the
  binary they run is the reviewed code.
- Releases signed (cosign / minisign), with an SBOM; the agent verifies signatures before self-update.
- The agent prints what it is allowed to do (`devbox-agent status`: console URL, account, mode, accepted
  console keys) and logs every session it serves locally, so the customer can check it without trusting us.
- The console side can stay closed source; the trust boundary is the agent and the protocol, both public.

### 6.3 Threat model notes
- The hosted console is a high-value target (it can run code on every enrolled machine). Mitigations:
  per-user console keys already exist (in Vault); add per-tenant isolation of the hub; audit log of every
  stream open (who, which host, when, from which browser session) visible to the customer.
- **Optional restricted mode** on the agent: allow only `tmux` and the console's file helpers, no free shell.
  The Shell button is then hidden for that host. Useful for cautious customers; default is full (Claude Code
  itself needs a shell anyway).
- The agent runs as the user, never root; it can only do what that user can.
- Agent binaries and updates are **signed** (minisign/cosign); the agent verifies before replacing itself.
  Auto-update can be turned off.

## 7. Changes in the console

- Host record: `kind: "ssh" | "agent"`; for agent hosts `agent_id` instead of `hostname/port`.
- `Box.ssh_base()`: for `kind == "agent"` add `-o ProxyCommand=devbox-proxy %n`, connect to `agent_id`,
  `HostKeyAlias=<agent_id>`, console key auth. Everything above `wrap()` stays as it is.
- Hub endpoints + `devbox-proxy` (Python script in the image is enough for a start; latency is dominated by the network).
- UI: "Agent" option in the host editor (install line, waiting for the agent…, online/offline dot, agent
  version, last seen), and "Revoke" on delete.
- "ask" mode and "browser" hosts don't apply to agent hosts (no password involved).

## 8. Running it as a service

- **Several console instances**: an agent's control channel lands on one instance. Either route by agent id
  (consistent hashing at the load balancer, or a small registry such as Redis: `agent_id → instance`) and let
  the hub forward stream requests to the right instance, or start with one instance per tenant.
- WebSocket idle timeouts on load balancers: ping every 20 s on the control channel.
- Metrics: agents online, reconnects / hour, stream open latency, bytes per stream; alert when an agent flaps.
- Versions: the hub reports outdated agents in the UI; the console stays compatible with the previous agent
  version for one release.

## 9. Phases and rough effort

| Phase | Scope | Effort |
|---|---|---|
| 0. Prototype (optional) | `ssh -R` from the host to a bastion container, host record points at `bastion:port` | 2–3 days |
| 1. Agent MVP | Go agent (SSH server over WSS, reconnect), hub, `devbox-proxy`, manual enrollment via CLI token; Linux + macOS; public repo + CI from day one | ~1.5 weeks |
| 2. Product | install scripts, systemd/launchd units, enrollment UI, revoke, online status, audit log | ~1 week |
| 2b. Windows | ConPTY, PowerShell installer, Scheduled Task, WSL option, winget package | ~1 week |
| 3. Hosted service | signed releases + auto-update, multi-instance routing, restricted mode, metrics | ~2 weeks |

Tests: the existing fake-SSH test setup gets a "fake agent" (the real agent binary started locally against
the test console), so `test_remote_hosts.py` can run the same flows over both transports.

## 10. Open questions

- Pricing / limits per agent (number of hosts, concurrent terminals).
- Native Windows: Claude sessions need tmux; offer WSL for those, or build a small session keeper of our own
  for Windows (keeps processes alive across disconnects the way tmux does)?
- License choice (Apache-2.0 gives an explicit patent grant; MIT is shorter).
- Do we want the customer to approve each new console key (e.g. a teammate added to the account), or is
  account membership enough?
- Data residency: stream bytes pass through our cloud (encrypted end-to-end by SSH, but metadata is ours).
