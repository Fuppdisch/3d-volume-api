# ---- Base ---------------------------------------------------------
FROM python:3.11-slim

# Konfigurierbare PrusaSlicer-Version (bei Bedarf im Render-UI als Build Arg setzen)
ARG PRUSA_VERSION=2.7.4

# Basis-ENV
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRUSASLICER_BIN="/opt/prusaslicer/usr/bin/prusa-slicer"

# Nützliche Tools + SquashFS für AppImage-Extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils squashfs-tools tini \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer installieren (AppImage extrahieren) --------------
# Versucht zuerst GTK3-Asset. Falls es die Datei-Namen leicht anders sind,
# fallback auf “GTK2”. (curl -fSL … || curl -fSL …)
RUN set -eux; \
    tmp="/tmp/ps.AppImage"; \
    (curl -fSL -o "$tmp" \
        "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}/PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK3.AppImage" \
     || curl -fSL -o "$tmp" \
        "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PRUSA_VERSION}/PrusaSlicer-${PRUSA_VERSION}+linux-x64-GTK2.AppImage"); \
    chmod +x "$tmp"; \
    "$tmp" --appimage-extract; \
    mv squashfs-root /opt/prusaslicer; \
    rm -f "$tmp"

# Bin in den PATH
ENV PATH="/opt/prusaslicer/usr/bin:${PATH}"

# ---- Python App ---------------------------------------------------
WORKDIR /app

# Abhängigkeiten
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Code
COPY app.py .

# ---- Runtime ------------------------------------------------------
EXPOSE 8000

# Healthcheck spricht deinen /health-Endpoint an
HEALTHCHECK --interval=30s --timeout=3s --retries=5 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# Tini als PID1 (sauberes Signal-Handling)
ENTRYPOINT ["/usr/bin/tini","--"]

# Uvicorn starten
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
