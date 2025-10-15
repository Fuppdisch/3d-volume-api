# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

ARG PRUSA_VERSION=2.7.4
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 QT_QPA_PLATFORM="offscreen"

SHELL ["/bin/sh","-c"]

# OS-Detect + Libs (unchanged)
RUN set -eux; \
  if [ -f /etc/alpine-release ] || command -v apk >/dev/null 2>&1; then \
    apk add --no-cache ca-certificates curl xz squashfs-tools tini \
      mesa-gl mesa-glu libxext libxrender glib libx11 libxcb libxrandr libxi libxfixes libxkbcommon \
      dbus-libs gtk+3.0 gdk-pixbuf pango cairo atk; \
  else \
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini \
      libgl1 libglu1-mesa libopengl0 \
      libx11-6 libx11-xcb1 libxcb1 libxcb-shm0 libxcb-render0 \
      libxrender1 libxrandr2 libxi6 libxfixes3 libxext6 libxkbcommon0 \
      libdbus-1-3 libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 \
      libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libatk1.0-0 \
    && rm -rf /var/lib/apt/lists/*; \
  fi

# PrusaSlicer (unchanged)
RUN set -eux; \
  tmp="/tmp/prusaslicer.AppImage"; \
  (curl -fSL -o "$tmp" \
      "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}/PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK3.AppImage" \
   || curl -fSL -o "$tmp" \
      "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}/PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK2.AppImage"); \
  chmod +x "$tmp"; "$tmp" --appimage-extract; mv squashfs-root /opt/prusaslicer; rm -f "$tmp"

ENV PRUSASLICER_BIN="/opt/prusaslicer/bin/prusa-slicer" \
    LD_LIBRARY_PATH="/opt/prusaslicer/usr/lib:/opt/prusaslicer/lib:${LD_LIBRARY_PATH}" \
    PATH="/opt/prusaslicer/bin:/opt/prusaslicer/usr/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
