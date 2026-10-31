# CamUI-WebRTC

A lightweight web interface for the Raspberry Pi camera module with WebRTC live stream, audio recording, media gallery, and multi-camera support. Built with Flask, PiCamera2, and MediaMTX.

## Requirements

- Raspberry Pi 4 or 5
- Raspberry Pi OS Bookworm (64-bit) or newer
- Raspberry Pi Camera Module (v2, v3, or HQ)
- Python 3.11+

## Dependencies

### System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y coturn python3-picamera2 python3-pip python3-venv openssl pipewire pipewire-pulse wireplumber
```

### MediaMTX

MediaMTX is the WebRTC/RTSP streaming server. This project is tested against **v1.16.2** — use this version to ensure compatibility:

```bash
wget https://github.com/bluenviron/mediamtx/releases/download/v1.16.2/mediamtx_v1.16.2_linux_arm64v8.tar.gz
tar -xzf mediamtx_v1.16.2_linux_arm64v8.tar.gz
sudo mv mediamtx /usr/local/bin/mediamtx
sudo chmod +x /usr/local/bin/mediamtx
```

## Installation

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

## Accessing the WebUI

Open a browser on any device on the same network and navigate to:

```
http://<hostname>.local
http://<ip-address>
```

The hostname is the Raspberry Pi's hostname (e.g. `http://pi-cam.local`).

## Service management

| Command | Description |
|---|---|
| `systemctl --user status mediamtx camui` | Show status of both services |
| `systemctl --user restart camui` | Restart the web application |
| `systemctl --user restart mediamtx` | Restart the streaming server |
| `systemctl --user stop camui mediamtx` | Stop both services |
| `journalctl --user -u camui -f` | Follow live logs of the web app |
| `journalctl --user -u mediamtx -f` | Follow live logs of MediaMTX |

## Configuration files

| File | Description |
|---|---|
| `system_settings.json` | Application settings (camera names, recording duration, theme, …) |
| `config/mediamtx/mediamtx.yml` | MediaMTX streaming server configuration |
| `config/coturn/turnserver.conf` | TURN server configuration for WebRTC relay |
| `config/systemd/camui.service` | Systemd user service for the web application |
| `config/systemd/mediamtx.service` | Systemd user service for MediaMTX |

## Compatibility

- **OS:** Raspberry Pi OS Bookworm (64-bit) or newer. Older releases (Bullseye) are not supported due to libcamera/PiCamera2 requirements.
- **Hardware:** Raspberry Pi 4 and 5. Pi 3B works but at lower frame rates. Pi Zero 2W is supported at reduced performance.
- **Camera:** Camera Module v2, v3, HQ. v1 is untested.

## Development

This project is developed with AI assistance. All contributors are expected to understand every line of code they submit — AI-generated or not. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contribution guidelines, git workflow, and PR requirements.

## License

MIT License. See [LICENSE](LICENSE) for details.
