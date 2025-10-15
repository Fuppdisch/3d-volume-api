# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

# --------- Build args / Ports -------------------------------------
ARG PRUSA_VERSION=latest
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# ---- System- & Runtime-Libs (OpenGL/X11/GTK) + Tools --------------
# Enthält: curl (Healthcheck & Downloads), xz/squashfs (AppImage-Extract),
# jq (nur temporär fürs API-Parsing), xvfb (virtuelles Display), tini (sauberes PID1)
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools jq tini xvfb \
      # OpenGL / X11 / GTK Runtime
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
      fonts-dejavu-core \
    ; \
    rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer AppImage dynamisch laden & entpacken --------------
# PRUSA_VERSION kann "latest" oder z.B. "2.9.3" sein
RUN set -eux; \
  apt-get update; apt-get install -y --no-install-recommends jq; \
  tmp="/tmp/prusaslicer.AppImage"; \
  api="https://api.github.com/repos/prusa3d/PrusaSlicer/releases"; \
  if [ "$PRUSA_VERSION" = "latest" ]; then api="$api/latest"; \
  else api="$api/tags/version_${PRUSA_VERSION}"; fi; \
  echo "Hole Release-Info von: $api"; \
  url="$(curl -fsSL "$api" \
        | jq -r '.assets[].browser_download_url' \
        | grep -iE 'AppImage$' \
        | head -n1)"; \
  test -n "$url" || (echo "Kein AppImage-Asset gefunden (PRUSA_VERSION=$PRUSA_VERSION)" && exit 22); \
  echo "Lade AppImage: $url"; \
  curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$url"; \
  chmod +x "$tmp"; \
  "$tmp" --appimage-extract; \
  mv squashfs-root /opt/prusaslicer; \
  rm -f "$tmp"; \
  # jq wieder entfernen + Cleanup
  apt-get purge -y jq && apt-get autoremove -y && apt-get clean; \
  rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer Pfade / Offscreen-Setup --------------------------
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    QT_QPA_PLATFORM="offscreen" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}" \
    DISPLAY=":99"

# ---- Python App ----------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# ---- Healthcheck & Ports ------------------------------------------
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# ---- Start (xvfb + uvicorn) ---------------------------------------
# xvfb stellt ein virtuelles Display bereit; tini ist sauberes PID1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["bash","-lc","xvfb-run -s '-screen 0 1024x768x24' uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
