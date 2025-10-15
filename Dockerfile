# =========================
# Base: Python API + OrcaSlicer (headless)
# =========================
FROM python:3.11-slim

# Du kannst diese URL bei Bedarf überschreiben:
ARG ORCASLICER_URL="https://github.com/SoftFever/OrcaSlicer/releases/download/v2.3.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.1.AppImage"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    # headless: kein Display nötig
    QT_QPA_PLATFORM=offscreen

# --- Systempakete / Runtime-Libs (OpenGL, X11, GTK, Tools) ---
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl jq xz-utils squashfs-tools tini \
      # OpenGL / X11 / GTK
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
      # optional: virtuelles Display
      xvfb \
    ; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# --- OrcaSlicer AppImage entpacken ---
# Ergebnis: ausführbares "orca-slicer" in /usr/local/bin + Libpfad gesetzt
ENV LD_LIBRARY_PATH="/opt/orca/usr/lib:/opt/orca/lib:${LD_LIBRARY_PATH}" \
    ORCASLICER_BIN="/usr/local/bin/orca-slicer" \
    PATH="/opt/orca/bin:/opt/orca/usr/bin:${PATH}"

RUN set -eux; \
    test -n "$ORCASLICER_URL"; \
    tmp="/tmp/orca.AppImage"; \
    echo ">> Lade OrcaSlicer von: $ORCASLICER_URL"; \
    curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$ORCASLICER_URL"; \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/orca; \
    rm -f "$tmp"; \
    # Binary robust lokalisieren und verlinken
    bin="$( (ls /opt/orca/usr/bin/* 2>/dev/null || true) | grep -Ei '(orca).*licer' | head -n1 )"; \
    if [ -z "$bin" ]; then bin="$( (ls /opt/orca/bin/* 2>/dev/null || true) | grep -Ei '(orca).*licer' | head -n1 )"; fi; \
    test -n "$bin" || (echo "Konnte OrcaSlicer-Binary nicht finden." && ls -R /opt/orca && exit 22); \
    ln -sf "$bin" /usr/local/bin/orca-slicer; \
    orca-slicer --version || true

# --- Python-App ---
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
# Falls eine GUI initialisiert werden möchte, nimm stattdessen:
# CMD ["xvfb-run","-a","uvicorn","app:app","--host","0.0.0.0","--port","8000"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
