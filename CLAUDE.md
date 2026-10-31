# CamUI-WebRTC — Claude Instructions

## Project overview

CamUI-WebRTC is a lightweight web interface for the Raspberry Pi camera module. It provides WebRTC live streaming, audio recording, media gallery, and multi-camera support. Built with Flask + gevent, PiCamera2, and MediaMTX as the WebRTC/RTSP server. Targets Raspberry Pi 4/5 running Raspberry Pi OS Bookworm (64-bit).

## Commands

**Setup:**
```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**Run (development):**
```bash
venv/bin/gunicorn \
  --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
  --workers 1 \
  --bind 0.0.0.0:80 \
  --chdir /path/to/CamUI-WebRTC \
  app:app
```

**Tests:**
```bash
python test_filename_gen.py
```

No linter or formatter config is currently set up.

## Structure

- `app.py` — Flask/SocketIO entry point; spawns `camera_server.py` on startup
- `camera_server.py` — Isolated picamera2 process (libav/RTSP); communicates via Unix socket
- `camera_client.py` — Unix socket RPC client; mirrors `CameraManager`/`Camera` interface transparently
- `camera_manager.py` — Camera business logic, profile and settings management
- `camera.py` — Camera object (controls, configs, recording state)
- `fmp4.py` — fMP4 parsing, virtual faststart MP4 streaming, HLS playlist generation
- `media_gallery.py` — Media file indexing, filtering, and gallery serving
- `system_settings.py` — System-level settings persistence
- `templates/` — Jinja2 HTML templates
- `static/` — CSS, JS, images; `static/gallery/` is gitignored (runtime data)
- `config/` — mediamtx, coturn, and systemd service configs

## Architecture

**Two-process design:** `app.py` runs in a gevent event loop (monkey-patched). PiCamera2's `PyavOutput` uses libav (C-level FFmpeg), whose blocking OS-thread calls cannot be made cooperative by gevent — running them in the same process deadlocks or silently breaks RTSP streaming. The subprocess boundary in `_ensure_camera_server()` solves this.

**Communication:** `camera_client.py` forwards every method call to `camera_server.py` over a Unix domain socket (`camera.sock`) using a JSON-RPC pattern. From `app.py`'s perspective all camera calls look local.

**Single gunicorn worker:** gevent's cooperative concurrency requires exactly one worker. Multiple workers would each spawn their own `camera_server.py` and compete for the camera hardware.

## Conventions

- After `venv/bin/pip install <pkg>`, immediately add the exact version to `requirements.txt` (alphabetical order):
  ```bash
  venv/bin/pip show <pkg> | grep "^Version:"
  ```
- When downloading an external JS library into `static/`, document it in `THIRD_PARTY.md` (version, path, source URL, license, download date).

## Testing

`test_filename_gen.py` tests `generate_media_filename()` with stub cameras. Run it before any PR:

```bash
python test_filename_gen.py
```

All 7 cases must pass. There are no other automated tests currently.

## Git workflow

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit format, PR requirements, and AI usage policy.

## Gotchas

- `config/systemd/camui.service` contains hardcoded `/home/pi/CamUI-WebRTC` paths. After copying to `~/.config/systemd/user/`, update `WorkingDirectory`, `ExecStart`, and `--chdir` if the repo is cloned elsewhere.
- `GeventWebSocketWorker` calls `monkey.patch_all()` before the app module is imported. The guard at the top of `app.py` prevents a double-patch warning — don't remove it.
- `camera.sock` is a runtime file created when `camera_server.py` starts; don't commit or manually edit it.

## Do not

- Do not add Python packages without updating `requirements.txt`.
- Do not bundle JS libraries without updating `THIRD_PARTY.md`.
- Do not run more than one gunicorn worker — it breaks camera access.
- Do not commit generated or runtime files (`camera.sock`, `camera_server.log`, `static/gallery/`).
