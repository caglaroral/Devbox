# devbox console

tmux içinde çalışan **Claude Code** (`claude`) oturumlarını, bu makinede ve istediğiniz sayıda uzak makinede tarayıcıdan (mobil dahil) yönetmek için bir web konsolu. Termius'un web arayüzüne benzer: soldaki **Hosts & sessions** ağacında makineleri ve oturumlarını görürsünüz. Her oturumun yanındaki nokta Claude Code'un o anki durumunu gösterir.

| Nokta | Anlamı |
|---|---|
| 🟢 nabız gibi atan | Claude Code çalışıyor (`esc to interrupt`, `✻ … (3m 19s · ↓ tokens)`) |
| 🟡 | Onay bekliyor (`Do you want to proceed?`, `❯ 1. Yes`) |
| ⚪ gri | Boşta |
| ○ içi boş | Durmuş (kayıtlı ama çalışmıyor) |
| 🔴 | claude kapandı (pencere açık, *Yeniden başlat* ile kalkar) |

## Kavramlar

- **Host:** "Bu makine" ya da SSH ile ulaşılan bir uzak makine. Ad, IP/host, port, kullanıcı ve kimlik bilgisiyle eklenir, düzenlenir, silinir.
- **Oturum:** Bir host'ta, `main` adlı tmux oturumunun içinde, seçilen **başlangıç klasöründe** çalışan bir `claude`. Kayıt olarak saklanır: görünen ad, tmux adı, sağlayıcı etiketi, klasör, komut, otomatik başlatma.
  - *Oluştur:* tmux penceresi açılır, `claude` o klasörde otomatik başlar.
  - *Devam:* `claude --continue` ile o klasördeki son konuşma sürdürülür.
  - *Kapat…* düğmesi sorar:
    - **Sadece görünümden kaldır:** Çalışmaya devam eder; "Gizli" listesinden geri getirilir.
    - **Durdur:** claude kapanır, kayıt kalır.
    - **Sil:** Kapanır ve kayıt silinir.
- **Harici oturumlar:** Makinedeki *tüm* tmux oturumları taranır. Konsol dışında açılmış pencereler (ör. `tmux new -s deneme`) "harici" olarak listelenir; izlenebilir, gizlenebilir, kapatılabilir.
- **Shell / SSH:** Seçili host'ta tmux dışında düz bir login shell açar. Eksik kurulumları (tmux, claude) buradan yapabilirsiniz.
- **Team:** Kullanıcının tüm host'larındaki oturumların ortak mesaj panosu. Oturumlar `devbox-msg post "…"` / `devbox-msg read` kullanır. Uzak oturumun adresi `ad@host-id` biçimindedir. "Oturuma da yaz" işaretlenirse mesaj `send-keys` ile pencereye yazılır.
- **Settings:** API anahtarları (oturumlara ortam değişkeni olarak verilir), yeni oturum varsayılanları, gizli bilgi deposu, konsolun SSH public key'i.

Terminal, ttyd yerine konsolun kendi PTY ⇄ WebSocket köprüsüyle xterm.js'e akar. Her tarayıcı sekmesi tmux'a ayrı bir *grouped session* olarak bağlanır; böylece her sekme farklı bir pencereyi gösterebilir, sekme kapanınca bu geçici görünüm silinir.

Kopyala/yapıştır:
- **Masaüstü:** Ctrl+Shift+C ile kopyala, Ctrl+V ile yapıştır; tmux copy-mode'dan OSC 52 ile kopyalama.
- **Dosya gönderme (📎 Attach):** Her tür dosya (resim, PDF, log, CSV…, en fazla 25 MB). Terminalde Ctrl+V ile yapıştır (ekran görüntüsü), dosyayı terminale sürükle ya da 📎 (mobilde dosya/galeri/kamera). Dosya session'ın çalıştığı klasörde `.devbox/uploads/` altına `<zaman>-<dosya adı>` olarak kaydedilir (git'e girmez, 7 gün sonra silinir) ve yolu prompt'a yazılır.
- **Event log:** Settings → Event log; terminal bağlantı/kopmaları, host erişimi, session'lar, yüklemeler, girişler. `<data>/users/<kullanıcı>/logs/` altında günlük dosyalar, 7 gün sonra silinir (`DEVBOX_LOG_DAYS`).
- **Mobil:** Terminale **uzun basınca** ekran metni yerel seçim tutamaçlarıyla açılır (Seçimi kopyala / Terminale yaz). Alttaki tuş çubuğunda Esc, Tab, Ctrl, oklar, ^C, ⇧Tab, PgUp/PgDn, Yapıştır ve ✎ Yaz (dikte/otomatik düzeltme için) bulunur. Kaydırma tmux copy-mode'u sürer, iki parmakla yazı boyutu değişir.

## Kurulum — Docker Compose (önerilen)

```bash
cd src
cp .env.example .env        # DEVBOX_PUBLIC_URL, DEVBOX_LOCAL_LABEL …
DEVBOX_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

`DEVBOX_COMMIT` isteğe bağlı; verilirse sol alttaki sürüm satırında (`devbox v0.2.0 · a05572c`) ve `/api/whoami` çıktısında görünür. Sürüm numarası `devbox_console/__init__.py` içindedir.

Host makinede gerekenler:
- `sshd` çalışıyor ve **parola ile giriş açık** olmalı. Giriş ekranı, kullanıcı adı/parolayı host'un sshd'sine (yani PAM'e) sordurur; Proxmox'taki "Linux PAM" girişi gibi.
- Her kullanıcı için `tmux` ve `claude` kurulu olmalı.

Konteyner, "bu makine"yi de `host.docker.internal:22` üzerinden SSH ile, giriş yapan kullanıcı olarak yönetir. İlk girişte o kullanıcının `~/.ssh/authorized_keys` dosyasına konsolun anahtarı eklenir. Parola saklanmaz.

Önüne TLS'li bir reverse proxy (ya da Tailscale) koyun. Pano kopyalama gibi tarayıcı API'leri HTTPS ister; HTTP'de de yedek yollar çalışır.

### Gizli bilgiler — HashiCorp Vault

Compose içindeki `vault` servisi (KV v2, `devbox/` mount) şunları tutar:
- API anahtarları / ortam değişkenleri
- Her kullanıcının konsol SSH özel anahtarı
- Host'lar için yapıştırılan özel anahtarlar
- **İsteğe bağlı olarak saklanan host parolaları** (host formunda "Parolayı Vault'ta sakla"; anahtar girişine izin vermeyen makineler için)

Özel anahtarlar yalnızca bağlantı anında tmpfs'teki (`/run/devbox`, RAM) 0600 dosyaya açılır.

- **Yönetilen mod (varsayılan):** Konsol ilk açılışta Vault'u init eder (1 anahtar payı). Unseal anahtarı ve root token'ı `console-data` volume'undaki `vault-init.json` (0600) dosyasına yazar. Her açılışta Vault'u unseal eder ve kendisi için yalnızca `devbox/` yolunu okuyup yazabilen bir token üretir.
  > ⚠️ Unseal anahtarı aynı makinede durduğu için bu mod, diskteki sırları Vault'un şifrelemesiyle korur; ama `console-data` volume'una erişen biri Vault'u açabilir. `vault-init.json` dosyasını yedekleyin ve isterseniz root token'ı oradan alıp başka bir yerde saklayın.
- **Harici Vault:** `compose.yaml`'da `VAULT_ADDR` + `VAULT_TOKEN` (+ `VAULT_MOUNT`) verin. Bu durumda init/unseal yapılmaz. Token'ın `<mount>/data/*` ve `<mount>/metadata/*` için create/read/update/delete yetkisi olmalı.
- `VAULT_ADDR` verilmezse sırlar `DATA/secrets/` altında 0600 JSON dosyalarda tutulur.

## Kurulum — doğrudan (Docker'sız, PAM)

```bash
pip install -r requirements.txt
./run.sh                    # tek kullanıcı: yalnızca çalıştıran kullanıcı girebilir
```

Çok kullanıcı için `devbox-console.service` dosyasıyla root olarak systemd servisi çalıştırın. PAM, giriş yapanın parolasını doğrular; tmux, claude ve shell süreçleri o kullanıcının uid'iyle başlatılır.

## Ortam değişkenleri

| Değişken | Varsayılan | |
|---|---|---|
| `DEVBOX_AUTH` | `pam` (Docker imajında `ssh`) | `pam`: in-process PAM, yerel süreçler · `ssh`: host'un sshd'si ile giriş, her şey SSH ile |
| `DEVBOX_LOCAL_SSH` | `host.docker.internal:22` | `ssh` modunda "bu makine" |
| `DEVBOX_LOCAL_LABEL` | hostname | Ağaçta görünen ad |
| `DEVBOX_HOST` / `DEVBOX_PORT` | `127.0.0.1` / `7700` | Dinleme adresi |
| `DEVBOX_PUBLIC_URL` | `http://HOST:PORT` | Oturumların panoya yazacağı adres (uzak makinelerden erişilebilir olmalı) |
| `DEVBOX_USERS` | (hepsi) | Girişine izin verilen kullanıcılar, virgülle |
| `DEVBOX_ALLOW_ROOT` | `0` | root girişi |
| `DEVBOX_TMUX_SESSION` | `main` | Oturumların tutulduğu tmux oturumu |
| `DEVBOX_POLL` | `4` | Durum yoklama aralığı (sn) |
| `DEVBOX_PROJECTS` | `~` | Klasör önerilerinin kökü (Settings'ten host başına değişir) |
| `DEVBOX_DEFAULT_CMD` | `claude` | Yeni oturum komutu |
| `DEVBOX_DATA` | `~/.local/share/devbox-console` (root: `/var/lib/devbox-console`, Docker: `/data`) | |
| `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_MOUNT` | — / — / `devbox` | Bkz. Vault |

## Dosyalar

```
src/
  devbox_console/app.py          sunucu: auth, host/oturum CRUD, tmux/SSH, PTY⇄WebSocket, SSE
  devbox_console/secretstore.py  Vault KV v2 / dosya deposu
  devbox_console/pam.py          libpam (ctypes)
  devbox_console/static/         index.html, app.js, app.css, login.html (xterm.js CDN'den)
  devbox-msg                     Team panosu CLI'ı (oturumlar için; /devbox-msg adresinden de indirilebilir)
  Dockerfile, compose.yaml, vault/vault.hcl, .env.example
  run.sh, devbox-console.service Docker'sız kurulum
```
