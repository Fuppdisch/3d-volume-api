# ---------- Dockerfile ----------
FROM python:3.11-slim

# Feste OrcaSlicer-Version (bei Bedarf per --build-arg überschreiben)
ARG ORCA_URL="https://github.com/SoftFever/OrcaSlicer/releases/download/v2.3.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.1.AppImage"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    # kompatible ENV-Variablen für bestehende Codes
    SLICER_BIN="/usr/local/bin/orca-slicer" \
    PRUSASLICER_BIN="/usr/local/bin/orca-slicer" \
    QT_QPA_PLATFORM="offscreen"

# --- Systemlibs & X-Stack (xvfb + xauth!) -----------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini \
      # OpenGL / X11 / GTK
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
      xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

# --- OrcaSlicer AppImage entpacken & verlinken -------------------------------
RUN set -eux; \
    tmp="/tmp/orca.AppImage"; \
    echo "Lade OrcaSlicer: $ORCA_URL"; \
    curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$ORCA_URL"; \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/orca; \
    ln -s /opt/orca/AppRun /usr/local/bin/orca-slicer; \
    echo 'export LD_LIBRARY_PATH="/opt/orca/usr/lib:/opt/orca/lib:${LD_LIBRARY_PATH}"' > /etc/profile.d/orca.sh

ENV LD_LIBRARY_PATH="/opt/orca/usr/lib:/opt/orca/lib:${LD_LIBRARY_PATH}" \
    PATH="/opt/orca/usr/bin:/opt/orca/bin:${PATH}"

# --- Python App ---------------------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

# Healthcheck pingt die FastAPI
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
