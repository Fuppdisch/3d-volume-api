# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

# --- Build args (Version/URL überschreibbar) ---
ARG PRUSA_VERSION=2.9.3
ARG PRUSASLICER_URL=""

# --- Meta & Defaults ---
LABEL org.opencontainers.image.title="volume-api" \
      org.opencontainers.image.description="API + PrusaSlicer headless" \
      org.opencontainers.image.prusaslicer="${PRUSA_VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    # Offscreen/QT
    QT_QPA_PLATFORM="offscreen" \
    # Wir nutzen Xvfb auf :99 (nur falls deine App ein DISPLAY braucht)
    DISPLAY=":99"

# ---- System & Runtime Libs für PrusaSlicer (Headless) -------------
# plus Xvfb + Tini für sauberes Signal-Handling
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini xvfb \
      # OpenGL / X11 / GTK Laufzeitlibs
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
      fontconfig \
    ; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer AppImage entpacken -------------------------------
# Robuster Downloader:
# 1) wenn PRUSASLICER_URL gesetzt ist -> genau die URL
# 2) sonst versuche gängige Dateinamen (GTK3/GTK2, +/−) unter den Tags "version_${PRUSA_VERSION}"
RUN set -eux; \
    tmp="/tmp/prusaslicer.AppImage"; \
    if [ -n "$PRUSASLICER_URL" ]; then \
      echo "Lade über direkte URL: $PRUSASLICER_URL"; \
      curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$PRUSASLICER_URL"; \
    else \
      base="https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}"; \
      echo "PRUSASLICER_URL nicht gesetzt. Versuche Standard-Muster unter: $base"; \
      set +e; found=""; \
      for f in \
        "PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK3.AppImage" \
        "PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK2.AppImage" \
        "PrusaSlicer-${PRUSA_VERSION}-linux-x64-GTK3.AppImage" \
        "PrusaSlicer-${PRUSA_VERSION}-linux-x64-GTK2.AppImage" \
        "PrusaSlicer-${PRUSA_VERSION}.linux-x86_64.AppImage" \
      ; do \
        echo "Versuche: $base/$f"; \
        if curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$base/$f"; then \
          echo "Gefunden: $f"; found="yes"; break; \
        fi; \
      done; \
      set -e; \
      if [ -z "$found" ]; then \
        echo "Konnte kein AppImage automatisch laden."; \
        echo "→ Entweder PRUSA_VERSION auf eine vorhandene Release-Version setzen"; \
        echo "  oder direkt PRUSASLICER_URL=<voller Download-Link> per --build-arg übergeben."; \
        exit 22; \
      fi; \
    fi; \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/prusaslicer; \
    rm -f "$tmp"

# WICHTIG: den echten Binary-Pfad verwenden (NICHT /usr/bin/prusa-slicer aus dem System)
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

# Mini-Smoke-Test (optional, failt nicht hart)
RUN set -eux; \
    if [ -x "$PRUSASLICER_BIN" ]; then \
      "$PRUSASLICER_BIN" --version || true; \
    fi

# ---- Python App ---------------------------------------------------
WORKDIR /app
# Falls du keine requirements.txt hast, lösche die beiden Zeilen:
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Deine App-Dateien:
COPY app.py .

# ---- Runtime: Entrypoint + Healthcheck ---------------------------
# Kleines Entrypoint-Skript: startet Xvfb (falls benötigt) und dann Uvicorn
RUN set -eux; \
    printf '%s\n' \
'#!/usr/bin/env bash' \
'set -euo pipefail' \
'echo "[entrypoint] starting Xvfb on ${DISPLAY}..."' \
'Xvfb "${DISPLAY}" -screen 0 1280x720x24 & ' \
'echo "[entrypoint] launching app..."' \
'exec uvicorn app:app --host 0.0.0.0 --port "${PORT}"' \
    > /usr/local/bin/entrypoint.sh; \
    chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/usr/local/bin/entrypoint.sh"]
