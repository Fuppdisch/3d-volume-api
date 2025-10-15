# ---- Base (Alpine) -----------------------------------------------
FROM python:3.11-alpine

ARG PRUSA_VERSION=2.7.4

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# ---- System-Libs (inkl. Fix für Alpine) --------------------------
# Kernpakete für OpenGL/X11/GTK + Tools zum Entpacken der AppImage
RUN apk add --no-cache \
    ca-certificates curl xz squashfs-tools tini \
    # "So fixt du das Deploy" Kernlibs
    mesa-gl mesa-glu libxext libxrender glib \
    # weitere Laufzeitlibs für PrusaSlicer
    mesa \
    libx11 libxcb libxrandr libxi libxfixes libxkbcommon \
    dbus-libs \
    gtk+3 gdk-pixbuf pango cairo atk

# ---- PrusaSlicer AppImage entpacken -------------------------------
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

# Wichtige Pfade/Env
ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    QT_QPA_PLATFORM="offscreen" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

# ---- Python App ---------------------------------------------------
WORKDIR /app

# (Optional) Build-Tools nur temporär, falls Wheels gebaut werden müssen
# Entferne den Block, wenn deine requirements reine Wheels sind.
COPY requirements.txt .
RUN apk add --no-cache --virtual .build-deps \
      build-base linux-headers python3-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apk del .build-deps

# Dein API Code (muss /health bedienen)
COPY app.py .

# Non-root User
RUN adduser -D -h /app worker && chown -R worker:worker /app
USER worker

# ---- Netzwerk/Health ---------------------------------------------
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# ---- Start --------------------------------------------------------
# In Alpine liegt tini üblicherweise unter /sbin/tini
ENTRYPOINT ["/sbin/tini","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
