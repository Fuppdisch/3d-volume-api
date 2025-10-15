# =========================
# Base / Python API + Headless PrusaSlicer (auto via GitHub API)
# =========================
FROM python:3.11-slim

ARG PRUSA_REPO="prusa3d/PrusaSlicer"          # GitHub Repo
ARG PRUSA_VERSION=""                          # z.B. 2.8.1  | leer => "latest"
ARG PRUSASLICER_URL=""                        # Direkte AppImage-URL (überschreibt API)
ARG FALLBACK_SLIC3R="yes"                     # "yes" => installiere slic3r, wenn kein AppImage im Release
# Optional: bei Rate-Limits: --build-arg GITHUB_TOKEN=ghp_xxx
ARG GITHUB_TOKEN=""

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    QT_QPA_PLATFORM=offscreen

# -------------------------
# Systempakete (OpenGL/X11/GTK + Tools)
# -------------------------
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl jq xz-utils squashfs-tools tini \
      # OpenGL / X11 / GTK Runtime
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
      # optional headless Display:
      xvfb \
    ; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# -------------------------
# PrusaSlicer via AppImage (Auto-Discovery) ODER Fallback Slic3r
# -------------------------
# Ergebnis: Entweder /opt/prusaslicer/bin/prusa-slicer existiert
#           ODER /usr/bin/slic3r als Fallback.
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

RUN set -eux; \
    tmp="/tmp/prusaslicer.AppImage"; \
    got="" ; \
    \
    if [ -n "$PRUSASLICER_URL" ]; then \
      echo ">> Verwende direkte URL: $PRUSASLICER_URL"; \
      curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$PRUSASLICER_URL" && got="url"; \
    else \
      # Release JSON holen (latest oder tag "version_<VER>")
      api="https://api.github.com/repos/${PRUSA_REPO}/releases"; \
      if [ -n "$PRUSA_VERSION" ]; then \
        api="${api}/tags/version_${PRUSA_VERSION}"; \
      else \
        api="${api}/latest"; \
      fi; \
      echo ">> Hole Release JSON: $api"; \
      auth=""; [ -n "$GITHUB_TOKEN" ] && auth="-H Authorization: token ${GITHUB_TOKEN}"; \
      json="$(curl -fsSL $auth "$api")"; \
      # Nach Linux-AppImage suchen
      url="$(echo "$json" | jq -r '.assets[]?.browser_download_url // empty' \
            | awk 'tolower($0) ~ /appimage/ && tolower($0) ~ /(linux|x86_64|x64)/ {print; exit}')" || true; \
      if [ -n "$url" ]; then \
        echo ">> Gefundenes AppImage: $url"; \
        curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$url" && got="api"; \
      else \
        echo "!! Kein Linux-AppImage im Release gefunden."; \
      fi; \
    fi; \
    \
    if [ -n "$got" ]; then \
      chmod +x "$tmp"; \
      "$tmp" --appimage-extract; \
      mv squashfs-root /opt/prusaslicer; \
      rm -f "$tmp"; \
      /opt/prusaslicer/bin/prusa-slicer --version || true; \
    else \
      if [ "$FALLBACK_SLIC3R" = "yes" ]; then \
        echo ">> Fallback aktiv: installiere slic3r aus Debian"; \
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends slic3r && \
        apt-get clean && rm -rf /var/lib/apt/lists/*; \
        # Auf Slic3r umschalten
        ln -sf /usr/bin/slic3r /usr/local/bin/prusa-slicer || true; \
        export PRUSASLICER_BIN="/usr/bin/slic3r"; \
        echo "Verwende Fallback-Binary: /usr/bin/slic3r"; \
        /usr/bin/slic3r --version || true; \
      else \
        echo "FEHLER: Kein AppImage gefunden und Fallback deaktiviert."; \
        echo "→ Setze PRUSASLICER_URL=<direkte AppImage-URL> ODER PRUSA_VERSION auf ein Release mit Linux-AppImage"; \
        exit 22; \
      fi; \
    fi

# -------------------------
# Python-App
# -------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
# Wenn du Probleme mit GUI-Initialisierung hast, nimm xfvb-run:
# CMD ["xvfb-run","-a","uvicorn","app:app","--host","0.0.0.0","--port","8000"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
