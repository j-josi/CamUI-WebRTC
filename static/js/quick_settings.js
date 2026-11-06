/**
 * Quick settings: which camera settings are offered in the gear menu of the live view.
 *
 * Per-browser preference (localStorage), shared by the camera page and Settings → General.
 * Modes per setting:
 *   "off"  – not in the gear menu (default)
 *   "both" – in the gear menu and still in the "Camera Settings" column
 *   "menu" – in the gear menu only; removed from the "Camera Settings" column
 */
(function () {
    var KEY = 'camui.quickSettings';
    var MODES = ['off', 'both', 'menu'];
    // Order = order in the gear menu. Must match the setting ids in camera_controls_db.json.
    var IDS = ['streaming_resolution', 'recording_resolution', 'still_capture_resolution', 'saveRAW'];

    function load() {
        try {
            var o = JSON.parse(localStorage.getItem(KEY) || '{}');
            return o && typeof o === 'object' ? o : {};
        } catch (e) { return {}; }
    }

    window.QuickSettings = {
        storageKey: KEY,
        ids: IDS,
        modes: MODES,
        get: function (id) {
            var m = load()[id];
            return MODES.indexOf(m) >= 0 ? m : 'off';
        },
        set: function (id, mode) {
            if (IDS.indexOf(id) < 0 || MODES.indexOf(mode) < 0) return;
            var o = load();
            o[id] = mode;
            try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (e) {}
        }
    };
})();
