# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

ARG PRUSA_VERSION=2.9.3

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DEBIAN_FRONTEND=noninteractive

# ---- System & Runtime Libs (OpenGL/X11/GTK + xvfb + tools) --------
RUN set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils squashfs-tools tini \
    # virtueller X-Server für headless GUI
    xvfb \
    # OpenGL / X11 / GTK Runtime
    libgl1 libglu1-mesa libopengl0 \
    libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
    libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
    libdbus-1-3 \
    libdrm2 libgbm1 \
    libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
    libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
  ; \
  apt-get clean; \
  rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer AppImage entpacken (robust gegen 404) ------------
# versucht mehrere übliche Dateinamen-Varianten (GTK3/GTK2, + oder -)
RUN set -eux; \
  tmp="/tmp/prusaslicer.AppImage"; \
  base="https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}"; \
  for f in \
    "PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK3.AppImage" \
    "PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK2.AppImage" \
    "PrusaSlicer-${PRUSA_VERSION}-linux-x64-GTK3.AppImage" \
    "PrusaSlicer-${PRUSA_VERSION}-linux-x64-GTK2.AppImage" \
  ; do \
    echo "Versuche $base/$f"; \
    if curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$base/$f"; then \
      echo "Gefunden: $f"; \
      break; \
    fi; \
  done; \
  test -s "$tmp" || (echo "Konnte kein PrusaSlicer AppImage für ${PRUSA_VERSION} laden. Setze ggf. ARG PRUSA_VERSION auf eine existierende Version (z.B. 2.9.3) oder prüfe Release-Assets." && exit 22); \
  chmod +x "$tmp"; \
  "$tmp" --appimage-extract; \
  mv squashfs-root /opt/prusaslicer; \
  rm -f "$tmp"

# Richtiger Binary-Pfad (nicht /usr/bin/prusa-slicer)
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer"

# Libpfad & Offscreen-Rendering; DISPLAY wird via Xvfb gesetzt
ENV LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    QT_QPA_PLATFORM="offscreen" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}" \
    DISPLAY=":99"

# ---- Python App ---------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

# Healthcheck: prüft die App und hält Render warm
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# ---- Start: tini + Xvfb + uvicorn -------------------------------
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["/bin/sh","-lc","Xvfb :99 -screen 0 1024x768x24 & exec uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
