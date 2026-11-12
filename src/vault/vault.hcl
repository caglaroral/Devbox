# Vault for devbox console (single node, file storage, internal network only).
# The console initialises and unseals it on first start (see devbox_console/secretstore.py).
storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1   # only reachable on the compose network; do not publish this port
}

api_addr      = "http://vault:8200"
disable_mlock = true
ui            = false
