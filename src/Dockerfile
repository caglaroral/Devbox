# devbox console — web UI for tmux + Claude Code sessions on N machines.
# In the container everything (including "this machine") is reached over SSH; login is checked by
# the host's sshd (PAM). Secrets live in Vault (see compose.yaml).
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin devbox \
 && mkdir -p /data /run/devbox && chown devbox:devbox /data /run/devbox

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY devbox_console ./devbox_console
COPY devbox-msg ./devbox-msg

ENV DEVBOX_AUTH=ssh \
    DEVBOX_HOST=0.0.0.0 \
    DEVBOX_PORT=7700 \
    DEVBOX_DATA=/data \
    DEVBOX_RUNTIME=/run/devbox \
    PYTHONUNBUFFERED=1

USER devbox
VOLUME ["/data"]
EXPOSE 7700
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:7700/api/whoami', timeout=4)" || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "devbox_console.app"]
