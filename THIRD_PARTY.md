# Third-Party Assets

This file documents all third-party libraries, data files, and external sources bundled with or used to generate assets in this project.

## JS Libraries

### Socket.IO

- Version: 4.8.1
- JS File: `static/js/socketio/socket.io.min.js`
- Source Map: `static/js/socketio/socket.io.min.js.map`
- JS URL: https://cdn.socket.io/4.8.1/socket.io.min.js
- Source Map URL: https://cdn.socket.io/4.8.1/socket.io.min.js.map
- License: MIT
- Downloaded: 2026-01-02

### hls.js

- Version: 1.6.16
- JS File: `static/hls.min.js`
- JS URL: https://cdn.jsdelivr.net/npm/hls.js@1.6.16/dist/hls.min.js
- License: Apache-2.0
- Downloaded: 2026-04-19

## Data Files

### Mains Electricity by Country

- Data File: `static/data/mains_frequency.json`
- Source: Wikipedia — *Mains electricity by country*
- Source URL: https://en.wikipedia.org/wiki/Mains_electricity_by_country
- License: CC BY-SA 4.0 (Wikipedia content)
- Fetch Script: `scripts/fetch_mains_frequency.py`
- Last Updated: 2026-11-01
- Description: Maps each country/territory to its AC mains frequency (50 or 60 Hz). Used by the anti-flicker exposure control in the camera settings. Regenerate with `python3 scripts/fetch_mains_frequency.py` from the repo root.
