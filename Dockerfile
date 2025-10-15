# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

# feste/optionale Metadaten
ARG PRUSA_VERSION=2.9.3
ARG PRUSASLICER_URL=""   # <- EXAKTE AppImage-URL hier per --build-arg übergeben

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    QT_QPA_PLATFORM="offscreen" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

# ---- System-Libs: OpenGL/X11/GTK + Tools/Xvfb ---------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini \
      # OpenGL / X11 / GTK
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
      xvfb \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer AppImage entpacken (URL Pflicht) -----------------
RUN set -eux; \
    test -n "$PRUSASLICER_URL" || (echo "FEHLER: PRUSASLICER_URL nicht gesetzt. Bitte exakte AppImage-URL per --build-arg PRUSASLICER_URL=... übergeben." && exit 22); \
    tmp="/tmp/prusaslicer.AppImage"; \
    echo "Lade $PRUSASLICER_URL"; \
    curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$PRUSASLICER_URL"; \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/prusaslicer; \
    rm -f "$tmp"

# ---- Python App ---------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

# Healthcheck (deaktiviert App-Hibernate-Fehlerdiagnose)
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
# Optional: mit Xvfb starten, falls PrusaSlicer doch ein Display möchte
CMD ["bash","-lc","xvfb-run -a uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
