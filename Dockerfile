FROM python:3.11-slim

# Tools zum Entpacken des AppImage
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils squashfs-tools tini && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# PrusaSlicer als AppImage (Version ggf. anpassen)
RUN curl -L -o /tmp/ps.AppImage \
    https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.7.2/PrusaSlicer-2.7.2+linux-x64-GTK3.AppImage \
    && chmod +x /tmp/ps.AppImage \
    && /tmp/ps.AppImage --appimage-extract \
    && mv squashfs-root /opt/prusaslicer \
    && rm -f /tmp/ps.AppImage

ENV PRUSASLICER_BIN=/opt/prusaslicer/usr/bin/prusa-slicer
ENV PRUSASLICER_TIMEOUT=180
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
