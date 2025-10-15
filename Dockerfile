# ---- Base (Debian/Ubuntu-Familie) --------------------------------
FROM python:3.11-slim

ARG PRUSA_VERSION=2.7.4

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# ---- System-Libs (inkl. Fix aus "So fixt du das Deploy") ----------
# - libGL & Freunde gegen "libGL.so.1 missing"
# - zusätzliche Laufzeitlibs für PrusaSlicer AppImage
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      # Basis
      ca-certificates curl xz-utils squashfs-tools tini \
      # "So fixt du das Deploy" Kernpakete
      libgl1 libglu1-mesa libxext6 libxrender1 libglib2.0-0 \
      # weitere X11/GL/GTK-Libs für PrusaSlicer
      libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrandr2 libxi6 libxfixes3 libxkbcommon0 \
      libdbus-1-3 \
      libgtk-3-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer AppImage entpacken -------------------------------
# Nimmt GTK3, fällt bei Bedarf auf GTK2 zurück
RUN set -eux; \
    tmp="/tmp/prusaslicer.AppImage"; \
    (curl -fSL -o "$tmp" \
        "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}/PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK3.AppImage" \
     || curl -fSL -o "$tmp" \
        "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}/PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK2.AppImage"); \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/prusaslicer; \
    rm -f "$tmp"

# Wichtig: echter Binary-Pfad
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer"

# Optional: Libpfad & Offscreen-Rendering (zieht kein DISPLAY)
ENV LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    QT_QPA_PLATFORM="offscreen" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

# ---- Python App ---------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Dein API Code (muss /health bedienen)
COPY app.py .

# Sicherheit: Non-root
RUN useradd -m worker && chown -R worker:worker /app
USER worker

# ---- Netzwerk/Health ---------------------------------------------
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# ---- Start --------------------------------------------------------
# Render setzt $PORT – wir benutzen tini als PID 1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
