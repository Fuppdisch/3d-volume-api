# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

ARG PRUSA_VERSION=2.7.4
# Optional: Direkter Download-Link zu einem AppImage (überschreibt Auto-Find)
ARG PRUSASLICER_URL=""

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# ---- System & Runtime Libs (OpenGL/X11/GTK + Tools) ---------------
RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini jq \
      # Headless OpenGL/X11/GTK Stack
      xvfb xauth \
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
    ; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer AppImage holen/entpacken ------------------------
# Strategie:
# 1) Wenn PRUSASLICER_URL gesetzt -> direkt laden
# 2) Sonst GitHub-API für "version_${PRUSA_VERSION}" abfragen und das erste *.AppImage nehmen
#    Wenn es keins gibt, mit sauberer Fehlermeldung abbrechen.
RUN set -eux; \
    tmp="/tmp/prusaslicer.AppImage"; \
    if [ -n "$PRUSASLICER_URL" ]; then \
      echo ">> Lade über direkte URL: $PRUSASLICER_URL"; \
      curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$PRUSASLICER_URL"; \
    else \
      echo ">> Suche AppImage-Asset via GitHub-API für version_${PRUSA_VERSION}"; \
      api="https://api.github.com/repos/prusa3d/PrusaSlicer/releases/tags/version_${PRUSA_VERSION}"; \
      url="$(curl -fsSL "$api" | jq -r '.assets[]?.browser_download_url | select(test("AppImage$"; "i"))' | head -n1 || true)"; \
      if [ -z "$url" ] || ! echo "$url" | grep -qi '\.AppImage$'; then \
        echo "FEHLER: Für version_${PRUSA_VERSION} wurde auf GitHub kein *.AppImage gefunden."; \
        echo "  > Entweder PRUSA_VERSION auf eine Version mit AppImage setzen (z.B. 2.7.4)"; \
        echo "  > oder eine direkte URL übergeben: --build-arg PRUSASLICER_URL=<voller-Link-zu-einem-AppImage>"; \
        exit 22; \
      fi; \
      echo ">> Gefundenes AppImage: $url"; \
      curl -fSL --retry 5 --retry-delay 2 -o "$tmp" "$url"; \
    fi; \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/prusaslicer; \
    rm -f "$tmp"

# Korrekte Binary- und Lib-Pfade aus dem entpackten AppImage
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    QT_QPA_PLATFORM="offscreen" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

# ---- Python App ---------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
