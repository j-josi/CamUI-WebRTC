# CamUI-WebRTC — Claude Instructions

## Project overview

CamUI-WebRTC is a lightweight web interface for the Raspberry Pi camera module. It provides WebRTC live streaming, audio recording, media gallery, and multi-camera support. Built with Flask + gevent, PiCamera2, and MediaMTX as the WebRTC/RTSP server. Targets Raspberry Pi 4/5 running Raspberry Pi OS Bookworm (64-bit).

## Priorities

1. **Video recording has the highest priority.** A recording must always run with the full configured frame rate and complete audio, no matter what else the camera does. No feature (extra live stream variants, additional encoders, previews, analysis, work triggered from the UI) may lower the recording fps or stall the recording. If resources run short, the recording wins.
2. **The live stream comes second.** Frame drops or a lowered live stream resolution for one or more clients are acceptable (and, if necessary, unavoidable).
3. Everything else (UI, statistics, convenience features) comes after that.

When changing the camera pipeline, encoders or streaming, check the effect on the recording: record for at least 30 s under the heaviest supported live stream setup and compare the frame rate of the file (`ffprobe -v error -select_streams v:0 -count_packets -show_entries stream=nb_read_packets:format=duration -of default=nw=1 <file>` → packets ÷ duration) with the configured fps. On the Raspberry Pi 4 at most 3 H.264 hardware encodes can run at the same time (recording included), and two 1080p encodes already come close to the limit.

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
- When adding a data file generated from an external source (e.g. `static/data/`), document it in `THIRD_PARTY.md` (data file path, source URL, license, generation script if any, date). If a generation script exists, reference it under `scripts/`. Do this in the same commit as the data file.

## i18n

All UI text must be translatable. The system is fully client-side after page load.

**Translation files** live in `static/i18n/`, one JSON file per language. Keys are dot-notation strings grouped by feature area (e.g. `"camera_controls.title"`, `"settings.tab_appearance"`). When adding new keys, add them to **all existing language files** in the same commit — never leave a language file incomplete.

**HTML attributes:**
- `data-i18n="key"` — sets `textContent`
- `data-i18n-html="key"` — sets `innerHTML` (for markup inside the label)
- `data-i18n-placeholder="key"` — sets the `placeholder` attribute on inputs
- `data-i18n-title="key"` — sets the `title` attribute (native browser tooltip)

**JS-side lookup:** `window.t('key')` returns the translated string, or `undefined` if the key is missing (does not throw). For interpolation, pass a substitution object: `window.t('key', { name: 'foo' })` — placeholders use `{name}` syntax in the JSON values.

**Dynamically injected HTML** (e.g. elements created with `innerHTML` or `appendChild`) is not auto-translated. Call `applyI18n(rootElement)` on the injected subtree after inserting it into the DOM.

**Wording of UI text:** Sentence case for labels, titles and radio options (`Audio delay – recording`, not `Audio Delay – Recording`); use an en dash with spaces (` – `) to separate a topic from its qualifier. Inline validation messages always start with `Value must …` and live under `validation.*`.

**Bootstrap tooltips** that need translation use `data-i18n-tooltip="key"` (custom attribute, not a standard Bootstrap attribute) and are initialised in JS after `applyI18n()` runs — the JS reads the attribute, calls `window.t()`, sets `title`, and then calls `new bootstrap.Tooltip(el)`.

## Input validation

Every user-editable value (text fields, number fields, slider number boxes) is checked in several stages:

1. **Keystroke filter** (JS) — only characters that can be part of a valid value can be typed/pasted. The character count is also limited to what the range allows (`-5000…0` → max. 5 characters), and for ranges allowing only values ≤ 0 a `-` is prepended automatically to non-zero numbers.
2. **Inline message** (JS) — an invalid value shows `Value must be in range between {min} and {max} {unit}` (or another `Value must …` sentence) directly below the field. Texts live in `static/i18n/*.json` under `validation.*`.
3. **Save-button guard** (JS) — a save button is only enabled while all active fields it saves are valid.
4. **Backend check** (`validation.py`) — every endpoint/socket handler that stores a value validates value *and* unit/range first and refuses invalid input (HTTP 400 `{error, field}` or the `setting_rejected` socket event).
5. **Visible failure** (JS) — a backend rejection is shown under the matching field (`InputValidation.showError`).

Frontend: `static/js/input_validation.js` is configured with `data-iv*` attributes on the `<input>` (see the header comment there) and `data-iv-guard="<selector>"` on the save button. Backend limits in `validation.py` must match the `data-iv-*` attributes in the templates. When adding a new input, wire all five stages and extend `tests/test_validation.py`.

## Desktop / Mobile

The UI is a **single responsive layout** built on Bootstrap 5.3.3 — there are no separate mobile templates. Use Bootstrap's grid and utility classes as the primary tool for responsive behaviour; add custom `@media` queries only when Bootstrap's breakpoints alone are insufficient.

**Interaction model:** The camera page is primarily designed for desktop use (mouse/hover). Where controls rely on hover or mousemove (e.g. the video overlay), this is intentional — do not replace that behaviour, but also do not introduce new hover-only interactions on elements that are expected to be fully usable on touch devices. When a new UI element must work on both, handle mouse and touch explicitly rather than relying on hover alone.

**Platform quirks:** iOS Safari does not support the standard Fullscreen API for `<video>` elements. The codebase already contains an iOS detection guard and falls back to the vendor-specific API — preserve this whenever fullscreen logic is touched.

**Layout duplication:** When an element must appear in a structurally different position on mobile vs. desktop (not just a size change), it is acceptable to render it twice in the DOM and toggle visibility with Bootstrap display utilities (`d-none d-md-flex` etc.). Do not abuse this pattern for purely aesthetic differences that CSS alone can handle.

## UI / UX conventions for settings controls

Reference implementation: the audio delay block in `templates/system_settings.html` (Settings → Recording & Audio). New settings controls follow the same patterns so the UI stays uniform.

**Option groups (radio buttons)**
- Radio options that belong together are wrapped in one `role="radiogroup"` container with a group title (small, semibold, sentence case, e.g. `Audio delay`) above the first option, referenced via `aria-labelledby`.
- Option labels are full sentences starting with a verb (`Use same audio delay for …`, `Use independent audio delays for …`). The label contains only the text — no icons or buttons.
- The configuration belonging to an option sits directly below its radio button, indented (`ps-4 mt-1`).
- The configuration of the inactive option stays visible but dimmed (`opacity-50`) and its inputs/buttons are `disabled`. Switching the option only toggles this state; it never discards values.

**Value fields (number with unit)**
- Layout per field: one row (`.setting-row` inside a `.setting-grid`, CSS in `system_settings.html`). Left column `.setting-label`: label (`form-label small mb-0`) followed by the info icon (`bi-info-circle text-muted` with `data-i18n-tooltip`). Right column `.setting-control`: input row, then the inline error row (→ optional warning). All rows of one grid share the label column, so the longest title decides where the inputs start; below `md` the label stacks above the control. Info icon always follows the field title, never a radio label. Rows belonging to a radio option get `ps-4` on the label.
- *Input row* (`d-flex align-items-center gap-2`): an `input-group input-group-sm` (~160 px) with a `−` button, the centred input (`form-control`) and a `+` button – the same pattern as the camera sliders – then the unit as `small text-muted` directly after the group, then the reset button. The buttons change the value by 1 per click and repeat while held, accelerating (`_spinStart`/`_spinStop` in `system_settings.html`); use pointer events so touch and mouse fire exactly once.
- *−/+ buttons* are disabled at the range limits; the *reset button* is `btn-outline-secondary` + `disabled` while the value equals the default and `btn-warning` + enabled otherwise.
- *Error row* directly under the input: `small text-danger`, space is reserved (`visibility:hidden` instead of `d-none`) so the layout does not jump. Wording and stages: see "Input validation".
- An additional range slider (e.g. volume) goes on its own line below the input row and error row (same width as the −/+ group), never in front of the input, so all inputs stay aligned.
- Warnings about side effects (e.g. fixed delay on the live stream) appear below the error row in `text-warning`, only while they apply.

**Save and unsaved changes**
- A tab has one Save button; it is disabled while any active field is invalid (`data-iv-guard`).
- Every setting of a tab must be part of its snapshot, so leaving the tab with unsaved edits opens the "Unsaved Changes" dialog.
- That dialog lists one line per change: `Cam0 – Audio delay: recording (0 ms → −2 ms)`. The label is `<scope> – <label with " – " replaced by ": ">`, followed by `(old → new)`. Changing only an option (radio) also creates a line, showing the option labels as old/new value. A dialog with an empty list is a bug.
- Settings that are not camera/profile parameters but only change presentation follow the "WebSocket sync" rules above.

## WebSocket sync for camera parameters

Every camera parameter that can be changed via the WebUI must be kept in sync across all connected clients via WebSocket. This includes both camera controls/configs and pure UI-only state.

**Camera controls and configs** (ExposureTime, AnalogueGain, hflip, etc.) are synced automatically: any change goes through `updateSettingFromElement` → `set_camera_setting` socket event → server applies → `handle_camera_setting_changed` → `camera_state` broadcast to all clients in the room.

**UI-only state** (switches and toggles that affect how a parameter is presented or constrained, but are not themselves camera parameters — e.g. the anti-flicker switch) must be synced explicitly:
- Client emits `set_ui_state` with `{ camera_num, key, value }`.
- Server stores it in `_camera_ui_states[camera_num][key]` and re-broadcasts as `ui_state` to all other clients in the room (`skip_sid=request.sid`).
- `_build_camera_state` includes `_camera_ui_states` under `state["ui_state"]`, so late-joining clients receive the current state on connect.
- `applyStateToUI` in `camera.html` handles `state.ui_state` by calling `applyUiState`.
- When applying a received ui_state, always pass `skipServerUpdate=true` to the handler to avoid feedback loops.

Key naming convention for UI state keys: `{settingId}_{uiElement}` (e.g. `ExposureTime_antiflicker`).

## Testing

Test files live in `tests/`. Run all tests before any PR:

```bash
python tests/test_filename_gen.py
python tests/test_system_settings.py
python tests/test_validation.py
```

| File | What it covers |
|---|---|
| `tests/test_filename_gen.py` | `CameraManager.generate_media_filename()` — naming, camera suffixes, collision counter |
| `tests/test_validation.py` | `validation.py` — range/unit checks, camera/profile/file names, system-settings payload |
| `tests/test_system_settings.py` | `SystemSettings` — first run, load, unknown-key filtering, scalar/dict updates, persistence, `max_recording_duration_s` |

Both use `unittest` and exit non-zero on failure. `tests/test_filename_gen.py` stubs `picamera2`, `camera`, and `media_gallery` via `sys.modules` so it runs without camera hardware. Each test file adds the repo root to `sys.path` so modules can be imported regardless of working directory.

### When to add tests

Add or extend a test file whenever new code meets **all** of the following criteria:

1. **Pure logic** — the function/method produces a deterministic output from its inputs, with no dependency on hardware (camera, GPIO, PulseAudio), live network sockets, or the filesystem beyond what a `tempfile` can provide.
2. **Non-trivial branching** — the code contains conditional logic, a collision-resolution loop, a merge strategy, or similar behaviour where an off-by-one or edge-case mistake would be silent and hard to spot in manual testing.
3. **Reachable without the full app** — the module can be imported in isolation (possibly with lightweight `sys.modules` stubs for hardware-bound imports, as done in `test_filename_gen.py`).

Typical candidates: filename generation, settings persistence, file-index/filter logic, fMP4/HLS parsing utilities, i18n interpolation helpers. Not candidates: anything that calls into PiCamera2, MediaMTX, WebSockets, or the browser.

### When to update existing tests

- **Code added to a tested module** — if the change adds a new branch or edge case to a function that already has tests, add a corresponding test case.
- **Code changed in a tested module** — if the behaviour of a tested function changes (different output for the same input, new parameter, renamed method), update the affected test cases to match. Never leave a test that asserts stale behaviour.
- **Code deleted from a tested module** — remove every test case that references the deleted function, method, or behaviour. A test that imports a name that no longer exists is a broken test, not a passing one.
- **Module renamed or moved** — update the import in the test file; do not leave a dead import.

Most of the application logic (PiCamera2, WebRTC, streaming, WebSockets) is tightly coupled to hardware and cannot be meaningfully unit-tested. When in doubt whether something warrants a test, apply the three criteria above rather than defaulting to "add a test" or "skip it".

## Git workflow

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch naming, commit format, PR requirements, and AI usage policy.

## Gotchas

- `config/systemd/camui.service` contains hardcoded `/home/pi/CamUI-WebRTC` paths. After copying to `~/.config/systemd/user/`, update `WorkingDirectory`, `ExecStart`, and `--chdir` if the repo is cloned elsewhere.
- `GeventWebSocketWorker` calls `monkey.patch_all()` before the app module is imported. The guard at the top of `app.py` prevents a double-patch warning — don't remove it.
- `camera.sock` is a runtime file created when `camera_server.py` starts; don't commit or manually edit it.

## Do not

- Do not add Python packages without updating `requirements.txt`.
- Do not bundle JS libraries or external data files without updating `THIRD_PARTY.md`.
- Do not run more than one gunicorn worker — it breaks camera access.
- Do not add a user-editable input without all five validation stages (see "Input validation").
- Do not invent a new layout for option groups or value fields; reuse the patterns in "UI / UX conventions for settings controls".
- Do not commit generated or runtime files (`camera.sock`, `camera_server.log`, `static/gallery/`).
