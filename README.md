# CamUI-WebRTC

A lightweight web interface for the Raspberry Pi camera module with WebRTC live stream, audio recording, media gallery, and multi-camera support. Built with Flask, PiCamera2, and MediaMTX.

## Requirements

- **Hardware:** Raspberry Pi 4 or 5 (Pi 3B works at lower frame rates, Pi Zero 2W at reduced performance)
- **OS:** Raspberry Pi OS Bookworm (64-bit) or newer — older releases (Bullseye) are not supported due to libcamera/PiCamera2 requirements
- **Camera:** Raspberry Pi Camera Module v2, v3 or HQ (v1 is untested)
- **Python:** 3.11+

## Prerequisites

### PipeWire

> **Skip this section** only if you plan to never use this project with a microphone connected to your Raspberry Pi (audio in recordings and live stream)

PipeWire running as a systemd user service enables microphone input for recording and live stream audio. Without it, the camera and live stream still work, just without audio.

**1. Install PipeWire (if missing).** Raspberry Pi OS Bookworm ships with PipeWire pre-installed. Check whether it is already running:

```bash
systemctl --user status pipewire
```

If it shows `active (running)`, continue with step 2. Otherwise install and start it:

```bash
sudo apt install -y pipewire pipewire-pulse wireplumber
systemctl --user enable --now pipewire pipewire-pulse wireplumber
```

**2. Enable lingering (only if CamUI should start automatically on boot).**

```bash
sudo loginctl enable-linger $USER
```

PipeWire runs as a systemd *user* service, which normally only runs while that user is logged in. This command makes the user's services start at boot and keep running without a login session. Without it, a CamUI that starts automatically on boot (see [Autostart on boot](#autostart-on-boot-docker-compose) for Docker Compose, step 8 of the manual installation for systemd) has no audio in the live stream and recordings until someone logs in. If you always start CamUI manually while logged in, you can skip this step.

### Docker & Docker Compose 

> **Skip this section** if either of the following applies:
> - You install via the manual (systemd) method and do not plan to modify the code. Docker is only required for the Docker Compose installation and for local development (`make run`, `make dev`).
> - Docker and Docker Compose are already installed and your user may run docker commands without `sudo`. Check with the command below: if docker and docker compose is already installed and the current user is able to run docker commands.
>
> ```bash
> docker compose version && docker ps
> ```
>
> **Skip ahead** if the output shows a Docker Compose version line (e.g. `Docker Compose version v2.x.x`) followed by the `docker ps` table header (`CONTAINER ID   IMAGE   COMMAND ...`), with or without running containers below it.
>
> **Do not skip** if you see one of these errors, and follow the steps below instead:
> - `docker: command not found` — Docker is not installed
> - `docker: 'compose' is not a docker command` — the Compose plugin is missing
> - `permission denied while trying to connect to the docker API` — Docker is installed, but your user is not (yet) in the `docker` group; if you just added it, reboot first

Install Docker and add your user to the `docker` group, which allows running docker commands without `sudo`. Docker Compose v2 is included as the `docker compose` plugin — no separate installation needed.

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Reboot the Raspberry Pi so that the group membership takes effect:

```bash
sudo reboot
```

---

## Installation

Choose one of two methods:

- **[Docker Compose](#installation-docker-compose)** — recommended; no manual dependency installation, all services run as containers, pre-built image pulled automatically
- **[Manual (systemd)](#installation-manual-systemd)** — install directly on the host, managed as systemd user services; requires installing all dependencies manually

### About coturn (recommended)

Both methods set up **coturn**, a local TURN server for the WebRTC live stream. It is optional, but recommended to ensure maximum browser compatibility:

- **Why:** Firefox hides its local IP address behind a random `.local` name that MediaMTX cannot resolve, so the live stream fails to connect. coturn relays the stream around this problem. Other browsers have not been verified without coturn.
- **Disable it only** if you do not need Firefox support (and possibly other browsers). The only gain is one service less to run; browsers that connect directly do not use the relay anyway.
- **How to disable:** for Docker Compose, remove the `coturn` service and the `coturn` entry under `depends_on` of `camui` in `docker/docker-compose.yml`. For the manual installation, run `sudo systemctl disable --now coturn`. In both cases also remove the `webrtcICEServers2` entry from `config/mediamtx/mediamtx.yml`. (Disabling has not been tested.)

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

By default the stack does not start on boot — start it manually with `docker compose up -d` and stop it with `docker compose down`. If a container crashes while the stack is running, it is restarted automatically.

#### Autostart on boot (Docker Compose)

To start the stack automatically whenever the Raspberry Pi boots, set the restart policy in `docker/.env`:

```
RESTART_POLICY=unless-stopped
```

and apply it with `docker compose up -d`. For audio after a reboot, also enable lingering (see [PipeWire](#pipewire), step 2). To go back to manual start, set `RESTART_POLICY=on-failure` (the default) and run `docker compose up -d` again.

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
python3 -m venv --system-site-packages venv
venv/bin/pip install -r requirements.txt
```

`--system-site-packages` is required so the venv can use the apt-installed `python3-picamera2` (installed in the dependencies step above).

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

Restart the coturn service so it picks up the configuration:

```bash
sudo systemctl restart coturn
```

> **Note:** installing the `coturn` package enables it at boot and starts it automatically. If you do not want coturn to start on boot, run `sudo systemctl disable coturn` (see also step 8).

### 5. Allow binding to port 80 without root

The application runs as an unprivileged user but needs to bind to port 80. Configure the kernel to allow this:

```bash
echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
```

### 6. Install systemd user services

Copy the service files from the repo to the user systemd directory:

```bash
mkdir -p ~/.config/systemd/user
cp config/systemd/mediamtx.service ~/.config/systemd/user/mediamtx.service
cp config/systemd/camui.service ~/.config/systemd/user/camui.service
systemctl --user daemon-reload
```

> **Note:** `~/.config/systemd/user/camui.service` contains hardcoded paths (e.g. `WorkingDirectory`, `ExecStart`, `--chdir`).
> If you cloned the repo to a different location than `/home/pi/CamUI-WebRTC`, update those paths accordingly.

### 7. Start the services

```bash
systemctl --user start mediamtx camui
```

Check that both are running:

```bash
systemctl --user status mediamtx camui
```

The services do not start on boot by default. After a reboot, start them again with the command above (and `sudo systemctl start coturn` if coturn is not running).

### 8. Autostart on boot (optional)

Skip this step if you prefer to start CamUI manually (and run `sudo systemctl disable coturn`, see step 4). To start everything automatically whenever the Raspberry Pi boots:

```bash
sudo systemctl enable coturn
systemctl --user enable mediamtx camui
sudo loginctl enable-linger $USER
```

The last command (lingering) is needed because user services otherwise only run while the user is logged in — see [PipeWire](#pipewire), step 2. To undo, replace `enable` with `disable` (and `disable-linger` for the last command).

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

---

## Contributing

Contributions are welcome.

### How to contribute

1. Fork the repository on GitHub
2. Clone your fork and create a branch from `main` for your change (e.g. `feat/<short-description>` or `fix/<short-description>`) — do not commit directly to `main`
3. Make your code changes
4. Before opening a pull request, verify both:
   - **Behaviour:** your change works as intended when [run on a Raspberry Pi](#testing-changes-locally)
   - **Tests:** all unit tests pass (`make test`); add or update tests if your change touches tested logic
5. Open a pull request from your branch against `main` of this repository — the maintainer reviews and merges
6. Wait for the review of a maintainer. Once your pull request is acctepted and merged, your change is included in the next release.

### Testing changes locally

All commands are run from the repository root. Run `make` to list all targets.

Only one instance can use the camera at a time. Before testing, stop any running CamUI deployment (`docker compose down` in `docker/`, or `systemctl --user stop camui mediamtx`).

#### Option A: run directly on the host (fast iteration)

```bash
make run
```

Starts the `mediamtx` and `coturn` containers and runs CamUI with gunicorn directly on the host. Code changes reload automatically. Ctrl+C stops gunicorn and both containers.

Requires the dependencies of the [manual installation](#installation-manual-systemd) (steps "Dependencies", "Clone" and "Create Python virtual environment"). The MediaMTX binary, the systemd services and the host coturn are not needed — Docker must be installed (see [Prerequisites](#docker)).

#### Option B: run in Docker (closest to a release)

```bash
make dev
```

Builds the CamUI image from local source (tagged `camui-webrtc:dev`, separate from the published `ghcr.io` image), starts the full stack and follows the logs. Ctrl+C stops all containers. Requires only Docker.

To switch back to the published image afterwards: `cd docker && docker compose up -d`.

#### Log level

Both targets accept `LOG=<level>` (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, case-insensitive):

```bash
make run LOG=info
make dev LOG=warning
```

Default: `ERROR` for `make run`, `DEBUG` for `make dev`.

#### Unit tests

```bash
make test
```

Runs the hardware-independent unit tests in `tests/`.

### Code guidelines

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit format, PR requirements, and AI usage policy.

---

## Development

This project is developed with AI assistance. All contributors are expected to understand every line of code they submit — AI-generated or not.

## License

MIT License. See [LICENSE](LICENSE) for details.
