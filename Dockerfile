FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Basis-Tools + jq für GitHub-API, squashfs-tools für AppImage-Extract, tini als Init
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils squashfs-tools jq tini \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---- PrusaSlicer laden (Version per TAG steuerbar) ----
ARG PS_TAG=version_2.7.4
RUN set -eux; \
    # 1) Asset-URL aus der GitHub-API herausfiltern (linux x64 GTK3 AppImage)
    APPIMAGE_URL="$(curl -sL https://api.github.com/repos/prusa3d/PrusaSlicer/releases/tags/${PS_TAG} \
      | jq -r '.assets[] | select(.name | test("linux-x64-GTK3.*AppImage$")) | .browser_download_url' \
      | head -n1)"; \
    test -n "$APPIMAGE_URL"; \
    echo "Downloading $APPIMAGE_URL"; \
    # 2) AppImage herunterladen und entpacken
    curl -L -o /tmp/ps.AppImage "$APPIMAGE_URL"; \
    chmod +x /tmp/ps.AppImage; \
    /tmp/ps.AppImage --appimage-extract; \
    mv squashfs-root /opt/prusaslicer; \
    # 3) Binary ins PATH verlinken
    ln -s /opt/prusaslicer/usr/bin/prusa-slicer /usr/local/bin/prusa-slicer; \
    rm -f /tmp/ps.AppImage

# Für die App sichtbar machen (wird in /health ausgegeben)
ENV PRUSA_SLICER=/usr/local/bin/prusa-slicer

# ---- Python-Abhängigkeiten ----
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- App-Code ----
COPY app.py .

# Uvicorn starten (tini als PID 1)
ENTRYPOINT ["tini","-g","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
