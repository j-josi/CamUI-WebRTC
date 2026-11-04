FROM debian:bookworm
LABEL maintainer="Jonas Josi"

RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg curl ca-certificates \
    && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
       | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] http://archive.raspberrypi.com/debian/ bookworm main" \
       > /etc/apt/sources.list.d/raspi.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python3-picamera2 \
        libcamera-apps \
        libcamera-tools \
        libcap-dev \
        libcamera-dev \
        libatlas-base-dev \
        libopenjp2-7 \
        libkms++-dev \
        libfmt-dev \
        libdrm-dev \
        ffmpeg \
        pulseaudio-utils \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY static /app/static
COPY templates /app/templates
COPY requirements.txt /app/
COPY *.py /app/
COPY *.json /app/

RUN pip install --break-system-packages --no-cache-dir -r requirements.txt

CMD ["gunicorn", \
     "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", \
     "--workers", "1", \
     "--bind", "0.0.0.0:80", \
     "--chdir", "/app", \
     "app:app"]
