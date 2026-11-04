# CamUI-WebRTC

A lightweight web interface for the Raspberry Pi camera module with WebRTC live stream, audio recording, media gallery, and multi-camera support. Built with Flask, PiCamera2, and MediaMTX.

## Requirements

- Raspberry Pi 4 or 5
- Raspberry Pi OS Bookworm (64-bit) or newer
- Raspberry Pi Camera Module (v2, v3, or HQ)
- Python 3.11+

## Prerequisites

### Audio support (optional)

Both installation methods require PipeWire running as a systemd user service to enable microphone input for recording and live stream audio. The camera and live stream work without audio if no microphone is connected.

Raspberry Pi OS Bookworm ships with PipeWire pre-installed. Check whether it is already running:

```bash
systemctl --user status pipewire
```

If active, nothing to do. If not installed:

```bash
sudo apt install -y pipewire pipewire-pulse wireplumber
systemctl --user enable --now pipewire pipewire-pulse wireplumber
sudo loginctl enable-linger pi
```

The last command (`enable-linger`) ensures PipeWire starts at boot even without an active login session.

### Docker (Docker Compose installation only)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Log out and back in (or run `newgrp docker`) so the group membership takes effect. Docker Compose v2 is included as the `docker compose` plugin — no separate installation needed.

---

## Installation

Choose one of two methods:

- **[Docker Compose](#installation-docker-compose)** — recommended; no manual dependency installation, all services run as containers, pre-built image pulled automatically
- **[Manual (systemd)](#installation-manual-systemd)** — install directly on the host, managed as systemd user services; requires installing all dependencies manually

---

## Installation: Docker Compose

### 1. Clone the repository

```bash
git clone https://github.com/j-josi/CamUI-WebRTC.git /home/pi/CamUI-WebRTC
cd /home/pi/CamUI-WebRTC
```

### 2. Create the gallery directory

Docker bind mounts require the host directory to exist before the container starts:

```bash
mkdir -p static/gallery
```

### 3. Stop any conflicting host services

If you previously installed coturn or MediaMTX directly on the host, stop them first — `network_mode: host` means containers share the host's network stack and will fail to bind if the ports are already taken:

```bash
sudo systemctl stop coturn
sudo systemctl disable coturn
sudo pkill mediamtx || true
```

### 4. Start the stack

```bash
cd docker
docker compose up -d
```

On the first run, Docker pulls the pre-built CamUI image (`ghcr.io/j-josi/camui-webrtc:latest`) and starts three containers: `mediamtx`, `coturn`, and `camui`. CamUI waits for MediaMTX and coturn to pass their health checks before starting. No build step is required.

Check that all containers are running:

```bash
docker compose ps
```

The stack restarts automatically on boot because all services are configured with `restart: unless-stopped`.

### Docker Compose service management

| Command | Description |
|---|---|
| `docker compose ps` | Show status of all containers |
| `docker compose logs -f camui` | Follow live logs of the web app |
| `docker compose logs -f mediamtx` | Follow live logs of MediaMTX |
| `docker compose restart camui` | Restart the web application |
| `docker compose down` | Stop and remove all containers |

---

## Installation: Manual (systemd)

### Dependencies

#### System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y coturn python3-picamera2 python3-pip python3-venv openssl
```

#### MediaMTX

MediaMTX is the WebRTC/RTSP streaming server. This project is tested against **v1.16.2** — use this version to ensure compatibility:

```bash
wget https://github.com/bluenviron/mediamtx/releases/download/v1.16.2/mediamtx_v1.16.2_linux_arm64v8.tar.gz
tar -xzf mediamtx_v1.16.2_linux_arm64v8.tar.gz
sudo mv mediamtx /usr/local/bin/mediamtx
sudo chmod +x /usr/local/bin/mediamtx
```

### 1. Clone the repository

```bash
git clone https://github.com/j-josi/CamUI-WebRTC.git /home/pi/CamUI-WebRTC
cd /home/pi/CamUI-WebRTC
```

### 2. Create Python virtual environment

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 3. Configure MediaMTX

Copy the bundled MediaMTX configuration to the system location:

```bash
sudo cp config/mediamtx/mediamtx.yml /usr/local/etc/mediamtx.yml
```

### 4. Configure coturn

Copy the bundled coturn configuration:

```bash
sudo cp config/coturn/turnserver.conf /etc/turnserver.conf
```

Enable and start the coturn service:

```bash
sudo systemctl enable --now coturn
```

### 5. Allow binding to port 80 without root

The application runs as an unprivileged user but needs to bind to port 80. Configure the kernel to allow this:

```bash
echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
```

### 6. Install systemd user services

Copy the service files from the repo to the user systemd directory and enable them:

```bash
mkdir -p ~/.config/systemd/user
cp config/systemd/mediamtx.service ~/.config/systemd/user/mediamtx.service
cp config/systemd/camui.service ~/.config/systemd/user/camui.service
systemctl --user daemon-reload
systemctl --user enable mediamtx camui
```

> **Note:** `~/.config/systemd/user/camui.service` contains hardcoded paths (e.g. `WorkingDirectory`, `ExecStart`, `--chdir`).
> If you cloned the repo to a different location than `/home/pi/CamUI-WebRTC`, update those paths accordingly.

### 7. Enable auto-start on boot (without login)

By default, user systemd services only run when the user is logged in. Enable **lingering** so the services start at boot even without an active session (e.g. headless / SSH not connected):

```bash
sudo loginctl enable-linger pi
```

### 8. Start the services

```bash
systemctl --user start mediamtx camui
```

Check that both are running:

```bash
systemctl --user status mediamtx camui
```

### Systemd service management

| Command | Description |
|---|---|
| `systemctl --user status mediamtx camui` | Show status of both services |
| `systemctl --user restart camui` | Restart the web application |
| `systemctl --user restart mediamtx` | Restart the streaming server |
| `systemctl --user stop camui mediamtx` | Stop both services |
| `journalctl --user -u camui -f` | Follow live logs of the web app |
| `journalctl --user -u mediamtx -f` | Follow live logs of MediaMTX |

---

## Accessing the WebUI

Open a browser on any device on the same network and navigate to:

```
http://<hostname>.local
http://<ip-address>
```

The hostname is the Raspberry Pi's hostname (e.g. `http://pi-cam.local`).

## Configuration files

| File | Description |
|---|---|
| `system_settings.json` | Application settings (camera names, recording duration, theme, …) |
| `config/mediamtx/mediamtx.yml` | MediaMTX streaming server configuration |
| `config/coturn/turnserver.conf` | TURN server configuration for WebRTC relay |
| `config/systemd/camui.service` | Systemd user service for the web application (manual install only) |
| `config/systemd/mediamtx.service` | Systemd user service for MediaMTX (manual install only) |

## Compatibility

- **OS:** Raspberry Pi OS Bookworm (64-bit) or newer. Older releases (Bullseye) are not supported due to libcamera/PiCamera2 requirements.
- **Hardware:** Raspberry Pi 4 and 5. Pi 3B works but at lower frame rates. Pi Zero 2W is supported at reduced performance.
- **Camera:** Camera Module v2, v3, HQ. v1 is untested.

---

## Contributing

Contributions are welcome.

### How to contribute

1. Fork the repository on GitHub
2. Clone your fork and make code changes
3. [Test your changes locally on a Raspberry Pi](#testing-changes-locally-with-docker)
4. Open a pull request — the maintainer reviews and merges
5. On merge to `main`, GitHub Actions automatically builds and publishes a new Docker image to ghcr.io

### Testing changes locally with Docker

A standalone `docker-compose.dev.yml` is provided for local development. It is fully self-contained and builds the CamUI image from local source, tagged as `camui-webrtc:dev` — completely separate from the published `ghcr.io` image.

```bash
cd docker
docker compose -f docker-compose.dev.yml up -d --build
```

Check logs:

```bash
docker compose -f docker-compose.dev.yml logs -f camui
```

To switch back to the published image:

```bash
docker compose -f docker-compose.dev.yml down
docker compose up -d
```

No cleanup needed — `camui-webrtc:dev` and `ghcr.io/j-josi/camui-webrtc:latest` are separate images and do not interfere.

### Code guidelines

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit format, PR requirements, and AI usage policy.

---

## Development

This project is developed with AI assistance. All contributors are expected to understand every line of code they submit — AI-generated or not.

## License

MIT License. See [LICENSE](LICENSE) for details.
