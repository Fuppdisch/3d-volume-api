# ------------------------------------------------------------
# Base
# ------------------------------------------------------------
FROM python:3.11-slim

# Allgemeine Python-Umgebung
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Uvicorn/HTTP-Port (Render erkennt das automatisch)
ENV PORT=8000

# ------------------------------------------------------------
# System-Pakete: curl + AppImage-Tools + PrusaSlicer-Runtime-Libs
# ------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils squashfs-tools tini \
    # --- RUNTIME LIBS FÜR PRUSASLICER (Headless/GTK/OpenGL/X11) ---
    libgtk-3-0 libglib2.0-0 libgdk-pixbuf-2.0-0 libpangocairo-1.0-0 \
    libpango-1.0-0 libcairo2 libatk1.0-0 libx11-6 libx11-xcb1 libxcb1 \
    libxcb-shm0 libxcb-render0 libxrender1 libxrandr2 libxi6 libxfixes3 \
    libxext6 libxkbcommon0 libdbus-1-3 libglu1-mesa libgl1 libopengl0 \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# PrusaSlicer installieren (AppImage entpacken)
# ------------------------------------------------------------
ARG PS_VERSION=2.7.2
RUN curl -L -o /tmp/prusaslicer.AppImage \
      "https://github.com/prusa3d/PrusaSlicer/releases/download/version_${PS_VERSION}/PrusaSlicer-${PS_VERSION}+linux-x64-GTK3.AppImage" \
 && chmod +x /tmp/prusaslicer.AppImage \
 && /tmp/prusaslicer.AppImage --appimage-extract \
 && mv squashfs-root /opt/prusaslicer \
 && rm -f /tmp/prusaslicer.AppImage

# Pfad zur Binärdatei für die App bekannt machen
ENV PRUSASLICER_BIN=/opt/prusaslicer/usr/bin/prusa-slicer

# ------------------------------------------------------------
# App-Code & Python-Abhängigkeiten
# ------------------------------------------------------------
WORKDIR /app

# zuerst nur requirements, damit Docker-Layer gecacht werden können
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# jetzt den Code
COPY app.py .

# ------------------------------------------------------------
# Laufzeit
# ------------------------------------------------------------
EXPOSE 8000

# kleiner Healthcheck (optional – Render hat eigenen Healthcheck, schadet aber nicht)
HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD \
  curl -fsS http://127.0.0.1:${PORT}/health || exit 1

# Tini als Init-Prozess (sauberes Signal-Handling)
ENTRYPOINT ["/usr/bin/tini", "--"]

# Uvicorn starten
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
