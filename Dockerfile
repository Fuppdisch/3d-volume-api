# ---------- Dockerfile ----------
FROM python:3.11-slim

# Feste OrcaSlicer-Version (bei Bedarf anpassen)
ARG ORCA_URL="https://github.com/SoftFever/OrcaSlicer/releases/download/v2.3.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.1.AppImage"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    # CLI-Pfad für die App
    SLICER_BIN="/usr/local/bin/orca-slicer" \
    PRUSASLICER_BIN="/usr/local/bin/orca-slicer" \
    QT_QPA_PLATFORM="offscreen" \
    XDG_RUNTIME_DIR="/tmp"

# Systemlibs für headless GUI/OpenGL/GTK + Xvfb + GStreamer + WebKit
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini xvfb xauth \
      # GL / X11
      libegl1 libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      # GTK / WebKit
      libdbus-1-3 libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
      webkit2gtk-4.1 \
      # GStreamer (teils von Orca genutzt)
      libgstreamer1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# OrcaSlicer AppImage entpacken und verlinken
RUN set -eux; \
    tmp="/tmp/orca.AppImage"; \
    echo "Lade OrcaSlicer: $ORCA_URL"; \
    curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$ORCA_URL"; \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/orca; \
    ln -s /opt/orca/AppRun /usr/local/bin/orca-slicer; \
    # LD_LIBRARY_PATH/ PATH ergänzen, damit gebündelte Libs gefunden werden
    echo 'export LD_LIBRARY_PATH="/opt/orca/usr/lib:/opt/orca/lib:${LD_LIBRARY_PATH}"' > /etc/profile.d/orca.sh

ENV LD_LIBRARY_PATH="/opt/orca/usr/lib:/opt/orca/lib:${LD_LIBRARY_PATH}" \
    PATH="/opt/orca/usr/bin:/opt/orca/bin:${PATH}"

# ---- Python App ---------------------------------------------------
WORKDIR /app

# 1) Requirements zuerst (Build-Cache optimal nutzen)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) App-Code
COPY app.py .

# 3) Profile ins Image kopieren (Schritt 4)
#    Erwartete lokale Struktur:
#    profiles/printers/*.json
#    profiles/process/*.json
#    profiles/filaments/*.json
COPY profiles /app/profiles

# Rechte (optional, aber sauber)
RUN find /app/profiles -type d -exec chmod 755 {} \; \
 && find /app/profiles -type f -exec chmod 644 {} \;

EXPOSE 8000

# Healthcheck (nutzt deine /health-Route)
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
