/**
 * Camera parameters panel (live view).
 *
 * Shows what the camera is doing, split into two clearly separated groups:
 *   LIVE  – values that are applied right now and change while the camera runs
 *           (libcamera frame metadata, active Picamera2 stream configuration)
 *   FIXED – what this camera hardware can do (control ranges, sensor data, sensor modes)
 *
 * Values are translated into plain language (label, unit, short hint). Raw technical
 * names stay available as tooltip, and the copy button exports the untouched raw JSON.
 * Translations: static/i18n/*.json under "params.*" and "live_view.parameters*".
 *
 * Usage: CameraParams.init({ socket, cameraNum, onUserAction })
 */
(function () {
  "use strict";

  const POLL_MS = 1000;

  // ---------------------------------------------------------------- helpers
  const tr = (key, fallback, subs) => {
    const v = window.t ? window.t(key, subs) : undefined;
    return v !== undefined ? v : fallback;
  };
  const round = (v, d) => {
    const f = Math.pow(10, d);
    return String(Math.round(v * f) / f);
  };
  const isNum = v => typeof v === "number" && isFinite(v);
  const yesNo = b => tr(b ? "params.yes" : "params.no", b ? "Yes" : "No");
  const prettify = s => String(s).replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2");

  // ------------------------------------------------- value formatters (by key)
  const enumOf = (key, names) => v => {
    const i = Number(v);
    return names[i] !== undefined ? tr("params.enum_" + key + "_" + i, names[i]) : String(v);
  };
  const msOf = us => isNum(us) ? (us >= 1000 ? round(us / 1000, 2) + " ms" : round(us, 0) + " µs") : String(us);
  const size2 = a => Array.isArray(a) && a.length >= 2 ? a[0] + " × " + a[1] + " px" : String(a);

  // Full formatters for the live / property display
  const FMT = {
    ExposureTime: v => isNum(v) && v > 0 && v < 1e6
      ? msOf(v) + "  (1/" + Math.round(1e6 / v) + " s)" : (isNum(v) ? round(v / 1e6, 2) + " s" : String(v)),
    AnalogueGain: v => "× " + round(v, 2),
    DigitalGain: v => "× " + round(v, 2),
    FrameDuration: v => isNum(v) && v > 0 ? round(1e6 / v, 1) + " fps" : String(v),
    FrameDurationLimits: a => Array.isArray(a) && a.length >= 2 && a[0] > 0 && a[1] > 0
      ? round(1e6 / a[1], 1) + " – " + round(1e6 / a[0], 1) + " fps" : String(a),
    ColourTemperature: v => round(v, 0) + " K",
    Lux: v => round(v, 0) + " lux",
    ColourGains: a => Array.isArray(a) && a.length >= 2
      ? tr("params.red", "red") + " × " + round(a[0], 2) + "   " + tr("params.blue", "blue") + " × " + round(a[1], 2)
      : String(a),
    LensPosition: v => isNum(v)
      ? (v > 0 ? round(v, 2) + " D  (≈ " + round(1 / v, 2) + " m)" : round(v, 2) + " D  (" + tr("params.infinity", "infinity") + ")")
      : String(v),
    ScalerCrop: a => Array.isArray(a) && a.length === 4
      ? a[2] + " × " + a[3] + " px  (" + tr("params.offset", "offset") + " " + a[0] + ", " + a[1] + ")" : String(a),
    ScalerCropMaximum: a => FMT.ScalerCrop(a),
    SensorTemperature: v => round(v, 1) + " °C",
    AfState: enumOf("AfState", ["Idle", "Scanning", "Focused", "Failed"]),
    AeState: enumOf("AeState", ["Idle", "Searching", "Converged"]),
    Location: enumOf("Location", ["Front", "Back", "External"]),
    ColorFilterArrangement: enumOf("ColorFilterArrangement", ["RGGB", "GRBG", "GBRG", "BGGR", "Mono"]),
    Rotation: v => v + "°",
    PixelArraySize: size2,
    UnitCellSize: a => Array.isArray(a) && a.length >= 2
      ? round(a[0] / 1000, 2) + " × " + round(a[1] / 1000, 2) + " µm" : String(a),
    size: size2,
    output_size: size2,
  };
  // Short scalar formatters for ranges (min / max / default of a control)
  const SCALAR = {
    ExposureTime: msOf,
    AnalogueGain: v => "× " + round(v, 2),
    DigitalGain: v => "× " + round(v, 2),
    ColourTemperature: v => round(v, 0) + " K",
    LensPosition: v => round(v, 2) + " D",
    FrameDurationLimits: msOf,
  };

  function formatGeneric(v) {
    if (v === null || v === undefined) return "–";
    if (typeof v === "boolean") return yesNo(v);
    if (isNum(v)) return Number.isInteger(v) ? String(v) : round(v, 4);
    if (Array.isArray(v)) return v.map(formatGeneric).join(", ");
    return String(v);
  }
  function formatValue(key, v) {
    const last = String(key).split(".").pop();
    const f = FMT[key] || FMT[last];
    if (f && v !== null && v !== undefined) {
      try { return f(v); } catch (_) { /* fall through */ }
    }
    return formatGeneric(v);
  }

  // --------------------------------------------------------- label lookup
  function labelFor(key) {
    const k = String(key);
    if (k.startsWith("controls.")) {
      return tr("params.requested", "Requested") + ": " + labelFor(k.slice(9));
    }
    return tr("params.names." + k.replace(/\./g, "_"), prettify(k.split(".").pop()));
  }
  const hintFor = key => tr("params.hints." + String(key).replace(/\./g, "_"), "");

  // ----------------------------------------------------- row model builders
  // A row: { key, label, hint, value (string) | lines (array of strings) }
  const HIDDEN_LIVE = new Set(["SensorTimestamp", "FrameWallClock"]);
  const LIVE_ORDER = ["ExposureTime", "AnalogueGain", "DigitalGain", "FrameDuration", "ColourTemperature",
    "ColourGains", "ColourCorrectionMatrix", "Lux", "LensPosition", "FocusFoM", "AfState", "AeState",
    "AeLocked", "ScalerCrop"];

  function flatten(value, prefix, out) {
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      Object.keys(value).forEach(k => flatten(value[k], prefix ? prefix + "." + k : k, out));
    } else {
      out.push([prefix, value]);
    }
  }
  const isMatrix9 = v => Array.isArray(v) && v.length === 9 && v.every(isNum);

  function rowFor(key, value) {
    const row = { key, label: labelFor(key), hint: hintFor(key) };
    if (isMatrix9(value)) {
      row.lines = [0, 3, 6].map(i => value.slice(i, i + 3).map(n => n.toFixed(4).padStart(8)).join("  "));
    } else if (Array.isArray(value) && value.some(x => x !== null && typeof x === "object")) {
      row.value = value.map(x => x !== null && typeof x === "object" ? JSON.stringify(x) : formatGeneric(x)).join("; ");
    } else {
      row.value = formatValue(key, value);
    }
    return row;
  }

  function rowsFromObject(obj, { hidden, order } = {}) {
    const flat = [];
    flatten(obj || {}, "", flat);
    let rows = flat.filter(([k]) => k && !(hidden && hidden.has(k))).map(([k, v]) => rowFor(k, v));
    const pos = k => (order && order.indexOf(k) >= 0) ? order.indexOf(k) : 1e6;
    rows.sort((a, b) => (pos(a.key) - pos(b.key)) || a.label.localeCompare(b.label, undefined, { numeric: true }));
    return rows;
  }

  // Mark which job (live stream / video recording) the main and lores streams currently have,
  // and whether that job is running right now.
  function withStreamRoles(rows, d) {
    const roles = d.stream_roles || {}, act = d.activity || {};
    rows.forEach(r => {
      const stream = r.key === "main.size" ? "main" : r.key === "lores.size" ? "lores" : null;
      const role = stream && roles[stream];
      if (role !== "recording" && role !== "streaming") return;
      const on = !!act[role];
      r.status = {
        text: tr("params.in_use_for", "Currently assigned to: {role}", { role: tr("params.role_" + role, role) }),
        color: on ? "#2ecc71" : "#ff4d4d",   // dot only: green = running, red = not running
      };
    });
    return rows;
  }

  // Controls: value is [min, max, default] -> "min – max · default d"
  function rowsFromControls(obj) {
    const rows = Object.keys(obj || {}).map(name => {
      const v = obj[name];
      const f = SCALAR[name] || formatGeneric;
      let value;
      if (Array.isArray(v) && v.length === 3) {
        value = f(v[0]) + " – " + f(v[1]) + "   (" + tr("params.default", "default") + " " + f(v[2]) + ")";
      } else {
        value = formatGeneric(v);
      }
      return { key: name, label: labelFor(name), hint: hintFor(name), value };
    });
    rows.sort((a, b) => a.label.localeCompare(b.label));
    return rows;
  }

  // Sensor modes: one row per mode; the one currently used by the stream is marked
  function rowsFromModes(modes, config) {
    const sensor = (config && config.sensor) || {};
    return (Array.isArray(modes) ? modes : []).map((m, i) => {
      const size = m.size || [];
      const inUse = Array.isArray(sensor.output_size) && sensor.output_size[0] === size[0]
        && sensor.output_size[1] === size[1] && sensor.bit_depth === m.bit_depth;
      const parts = [size[0] + " × " + size[1] + " px"];
      if (m.bit_depth) parts.push(tr("params.bit", m.bit_depth + "-bit", { n: m.bit_depth }));
      if (isNum(m.fps)) parts.push(tr("params.up_to_fps", "up to " + round(m.fps, 1) + " fps", { fps: round(m.fps, 1) }));
      return {
        key: "mode" + i,
        label: tr("params.mode", "Mode " + (i + 1), { n: i + 1 }),
        hint: m.format ? String(m.format) : "",
        value: parts.join(" · "),
        badge: inUse ? tr("params.in_use", "in use") : "",
      };
    });
  }

  // ------------------------------------------------------------- rendering
  const SECTIONS = {
    live: [
      { id: "metadata", source: "libcamera",  build: d => rowsFromObject(d.metadata, { hidden: HIDDEN_LIVE, order: LIVE_ORDER }) },
      { id: "config",   source: "Picamera2",  build: d => withStreamRoles(rowsFromObject(d.configuration), d) },
    ],
    fixed: [
      { id: "controls",   source: "libcamera", build: d => rowsFromControls(d.camera_controls) },
      { id: "properties", source: "libcamera", build: d => rowsFromObject(d.camera_properties) },
      { id: "modes",      source: "Picamera2", build: d => rowsFromModes(d.sensor_modes, d.configuration) },
    ],
  };
  const GROUPS = {
    live:  { dot: "#2ecc71", icon: "bi-activity" },
    fixed: { dot: "#adb5bd", icon: "bi-lock" },
  };

  const rendered = {};   // container id -> signature (skip DOM rebuild when unchanged)

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function buildSkeleton(root) {
    root.textContent = "";
    ["live", "fixed"].forEach((g, gi) => {
      const accent = GROUPS[g].dot;
      const wrap = el("section", gi ? "mt-5" : "");
      // Group header: large, solid bar with accent colour
      const head = el("div", "px-2 py-2 rounded-top d-flex align-items-center gap-2 fw-bold");
      head.style.fontSize = "1rem";
      head.style.background = "rgba(255,255,255,0.18)";
      head.style.borderLeft = "5px solid " + accent;
      const ic = el("i", "bi " + GROUPS[g].icon);
      ic.style.color = accent;
      const title = el("span");
      title.setAttribute("data-params-group-title", g);
      head.append(ic, title);
      const desc = el("div", "px-2 py-1 mb-2 text-white-50");
      desc.style.fontSize = "0.72rem";
      desc.style.background = "rgba(255,255,255,0.08)";
      desc.style.borderLeft = "5px solid " + accent;
      desc.setAttribute("data-params-group-desc", g);
      wrap.append(head, desc);
      SECTIONS[g].forEach(s => {
        // Section header: small caps label + source tag, underlined, with space above
        const h = el("div", "d-flex align-items-center gap-2 mt-4 mb-1 pb-1");
        h.style.borderBottom = "1px solid " + accent;
        h.setAttribute("data-params-section", s.id);
        const label = el("span", "text-uppercase fw-semibold");
        label.style.fontSize = "0.7rem";
        label.style.letterSpacing = "0.06em";
        label.style.color = accent;
        label.setAttribute("data-params-section-title", s.id);
        const src = el("span", "badge rounded-pill fw-normal text-white-50", s.source);
        src.style.border = "1px solid rgba(255,255,255,0.3)";
        h.append(label, src);
        // Rows belong visually to their section: indented with a thin accent line
        const body = el("div", "ms-1 ps-2");
        body.style.borderLeft = "2px solid " + accent;
        body.id = "params_" + s.id;
        wrap.append(h, body);
      });
      root.appendChild(wrap);
    });
    applyTexts(root);
  }

  function applyTexts(root) {
    root.querySelectorAll("[data-params-group-title]").forEach(e => {
      const g = e.getAttribute("data-params-group-title");
      e.textContent = tr("params.group_" + g + "_title", g === "live" ? "Live Camera Parameters" : "Camera specifications");
    });
    root.querySelectorAll("[data-params-group-desc]").forEach(e => {
      const g = e.getAttribute("data-params-group-desc");
      const text = tr("params.group_" + g + "_desc", "");
      e.textContent = text;
      e.style.display = text ? "" : "none";   // no description -> no empty bar
    });
    Object.keys(SECTIONS).forEach(g => SECTIONS[g].forEach(s => {
      const h = root.querySelector('[data-params-section-title="' + s.id + '"]');
      if (h) h.textContent = tr("params.section_" + s.id, s.id);
    }));
  }

  function renderRows(containerId, rows) {
    const box = document.getElementById(containerId);
    const sig = JSON.stringify(rows);
    if (rendered[containerId] === sig) return;
    rendered[containerId] = sig;
    box.textContent = "";
    if (!rows.length) {
      box.appendChild(el("span", "text-white-50", tr("live_view.parameters_empty", "No data available")));
      return;
    }
    const frag = document.createDocumentFragment();
    rows.forEach(r => {
      const row = el("div", "py-1 border-bottom border-secondary border-opacity-25");
      const line = el("div", "d-flex justify-content-between gap-3");
      const left = el("div");
      const name = el("div", "", r.label);
      name.title = r.key;            // raw technical name for experts
      left.appendChild(name);
      if (r.hint) {
        const h = el("div", "text-white-50", r.hint);
        h.style.fontSize = "0.65rem";
        h.style.lineHeight = "1.25";
        left.appendChild(h);
      }
      if (r.status) {
        const st = el("div");
        st.style.fontSize = "0.68rem";
        const dot = el("span", "", "●");
        dot.style.color = r.status.color;
        dot.className = "me-1";
        st.append(dot, r.status.text);
        left.appendChild(st);
      }
      line.appendChild(left);
      if (r.lines) {
        row.appendChild(line);
        const pre = el("div", "text-end font-monospace", r.lines.join("\n"));
        pre.style.whiteSpace = "pre";
        row.appendChild(pre);
      } else {
        const val = el("div", "text-end fw-semibold text-break", r.value);
        val.style.flex = "0 1 55%";
        if (r.badge) {
          const b = el("span", "badge bg-success ms-2 fw-normal", r.badge);
          val.appendChild(b);
        }
        line.appendChild(val);
        row.appendChild(line);
      }
      frag.appendChild(row);
    });
    box.appendChild(frag);
  }

  function renderAll(data) {
    // Name the sensor in the "Sensor properties" section title (the Model is a sensor property)
    const model = data && data.camera_properties && data.camera_properties.Model;
    const pt = document.querySelector('[data-params-section-title="properties"]');
    if (pt) {
      const base = tr("params.section_properties", "Sensor properties");
      pt.textContent = model ? base + " – " + model : base;
    }
    Object.keys(SECTIONS).forEach(g => SECTIONS[g].forEach(s => {
      let rows = [];
      try { rows = s.build(data || {}); } catch (e) { console.warn("[params]", s.id, e); }
      renderRows("params_" + s.id, rows);
    }));
  }

  // --------------------------------------------------------------- clipboard
  // navigator.clipboard needs a secure context (https/localhost); the app is usually served
  // via plain http on the LAN -> textarea + execCommand fallback.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise((resolve, reject) => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed; top:0; left:0; opacity:0;";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) {}
      ta.remove();
      ok ? resolve() : reject(new Error("copy failed"));
    });
  }

  // -------------------------------------------------------------------- init
  function init({ socket, cameraNum, onUserAction }) {
    const btn = document.getElementById("paramsBtn");
    const panel = document.getElementById("paramsPanel");
    const content = document.getElementById("paramsContent");
    const copyBtn = document.getElementById("paramsCopyBtn");
    let timer = null, busy = false, last = null, copyTimer = null;

    const isOpen = () => panel.style.display !== "none";

    function refresh() {
      if (busy || document.hidden) return;
      busy = true;
      socket.emit("get_camera_parameters", { camera_num: cameraNum }, data => {
        busy = false;
        if (!isOpen()) return;
        last = data || null;
        renderAll(last);
      });
      setTimeout(() => { busy = false; }, 3000);   // never stay busy if the ack is lost
    }

    function setOpen(open) {
      panel.style.display = open ? "block" : "none";
      btn.setAttribute("aria-pressed", open ? "true" : "false");
      btn.classList.toggle("active", open);
      clearInterval(timer);
      timer = null;
      if (open) {
        if (!content.firstChild) buildSkeleton(content);
        refresh();
        timer = setInterval(refresh, POLL_MS);
      }
    }

    btn.addEventListener("click", e => {
      e.stopPropagation();
      setOpen(!isOpen());
      if (onUserAction) onUserAction();
    });

    copyBtn.addEventListener("click", e => {
      e.stopPropagation();
      const d = last || {};
      const text = JSON.stringify({
        libcamera_frame_metadata: d.metadata || {},
        picamera2_stream_configuration: d.configuration || {},
        libcamera_camera_controls: d.camera_controls || {},
        libcamera_camera_properties: d.camera_properties || {},
        picamera2_sensor_modes: d.sensor_modes || [],
        camui_stream_roles: d.stream_roles || {},
        camui_activity: d.activity || {},
      }, null, 2);
      copyText(text).then(() => {
        copyBtn.innerHTML = '<i class="bi bi-check2"></i>';
        copyBtn.title = tr("live_view.parameters_copied", "Copied");
        clearTimeout(copyTimer);
        copyTimer = setTimeout(() => {
          copyBtn.innerHTML = '<i class="bi bi-clipboard"></i>';
          copyBtn.title = tr("live_view.parameters_copy", "Copy to clipboard");
        }, 2000);
      }).catch(() => {});
    });
  }

  window.CameraParams = { init };
})();
