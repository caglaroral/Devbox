# Devbox

Tarayıcıdan (mobil dahil) **Claude Code** oturumlarını yöneten web konsolu. Her oturum, seçilen bir başlangıç klasöründe tmux içinde çalışan bir `claude`'dur. Oturumlar bu makinede ya da SSH ile bağlanılan istediğiniz sayıda uzak makinede olabilir. Termius'un web arayüzüne benzer.

- **Giriş:** Makinenin Linux kullanıcısıyla (PAM), Proxmox'taki gibi. Shell ve oturumlar o kullanıcı olarak çalışır.
- **Hosts & sessions ağacı:** Host ve oturum ekleme/düzenleme/silme. Her oturumun yanındaki nokta Claude Code'un durumunu gösterir: 🟢 çalışıyor · 🟡 onay bekliyor · ⚪ boşta.
- **Kapat… düğmesi:** "Sadece görünümden kaldır" (oturum çalışmaya devam eder, geri getirilebilir), "durdur" ya da "sil". Makinedeki konsol dışında açılmış tmux oturumları da listelenir.
- **Shell / SSH:** Her host'ta düz bir shell; kurulumlar buradan yapılabilir.
- **Team:** Oturumların birbirine ve size yazdığı mesaj panosu (`devbox-msg`).
- **Settings:** API anahtarları, varsayılanlar, konsolun SSH anahtarı.
- **Gizli bilgiler:** Host parolaları/anahtarları ve API anahtarları HashiCorp Vault'ta tutulur.
- **Görünüm:** GitHub renkleri; mobilde çekmece menü, tuş çubuğu, kopyala/yapıştır.

## Hızlı başlangıç

```bash
cd src
cp .env.example .env
DEVBOX_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build     # http://<makine>:7700
```

Host makinede `sshd` (parola ile giriş açık), `tmux` ve `claude` kurulu olmalı. Docker'sız kurulum, Vault modları ve tüm ayarlar için: **[src/README.md](src/README.md)**.

## Depo yapısı

```
src/      uygulama (aiohttp sunucu + statik arayüz), Dockerfile, compose.yaml, Vault ayarı
tests/    pytest testleri, sahte ssh, tarayıcı testleri (Playwright)
```

## Testler

Gerçek sunucuyu boş bir portta, geçici bir veri klasörü ve ayrı tmux soketleriyle başlatır. Uzak makineler için `tests/fakebin/ssh` kullanılır: komutu yerelde ayrı bir HOME ile çalıştırır, sshd gerekmez. Testlerin herhangi bir arka kapısı yok; oturum çerezi test veri klasörünün kendi anahtarıyla imzalanır.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # tmux gerekir
```

| Dosya | Kapsam |
|---|---|
| `tests/test_status.py` | Durum noktası sezgisi (çalışıyor / onay bekliyor / boşta) |
| `tests/test_api.py` | Giriş/Origin kontrolü, oturum CRUD, başlangıç klasörü, API anahtarları, gizle/geri getir, harici tmux oturumları, kesme/yeniden başlatma/yazma, Team panosu |
| `tests/test_ws.py` | Terminal WebSocket'i: tmux görünümü, giriş, boyut değiştirme, kaydırma, düz shell |
| `tests/test_remote_hosts.py` | Uzak host CRUD, parola ile anahtar kurulumu, uzak oturumlar, mesajlar, SSH shell |
| `tests/test_vault.py` | Gerçek Vault: init/unseal, kısıtlı token, diskte düz metin sır olmaması, yeniden başlatma (Docker gerekir, yoksa atlanır) |
| `tests/test_browser.py` | Masaüstü + mobil arayüz duman testi: JS hatası, yatay taşma, gizle akışı (`cd tests/browser && npm install` gerekir). Bellek sızıntısı/CPU ölçümü: `DEVBOX_PERF=1 pytest tests/test_browser.py` |

Gerçek `claude` ve gerçek sshd/PAM girişi testlerde taklit edilir; ilk kurulumda bunları elle doğrulayın.
