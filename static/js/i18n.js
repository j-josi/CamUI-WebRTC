/**
 * CamUI i18n — client-side translation helper.
 *
 * Language priority: localStorage('camui_lang') → window._i18nDefaultLang → 'en'
 *
 * Supported attributes on elements:
 *   data-i18n="dot.path.key"         → sets element.textContent
 *   data-i18n-html="dot.path.key"    → sets element.innerHTML
 *   data-i18n-placeholder="..."      → sets element.placeholder
 *   data-i18n-title="..."            → sets element.title
 *
 * Language switch: window.setLanguage('de') — saves to localStorage, reloads page.
 * Translation lookup: window.t('dot.path.key') — returns value or undefined.
 */
(function () {
    var serverDefault = (typeof window._i18nDefaultLang !== 'undefined')
        ? window._i18nDefaultLang : 'en';
    var lang = localStorage.getItem('camui_lang') || serverDefault;

    // Load language file synchronously so translations are ready at DOMContentLoaded
    var translations = {};
    try {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/static/i18n/' + lang + '.json', false); // synchronous
        xhr.send();
        if (xhr.status === 200) {
            translations = JSON.parse(xhr.responseText);
        }
    } catch (e) {
        console.warn('[i18n] Failed to load language file for "' + lang + '":', e);
    }

    window._i18n    = translations;
    window._i18nLang = lang;

    /**
     * Resolve a dot-notation key from the translations object.
     * e.g. t('navbar.live_view') → translations.navbar.live_view
     * Returns undefined when any segment is missing.
     *
     * Optional second argument `subs` is a plain object whose keys replace
     * {placeholders} in the returned string, e.g.:
     *   t('live_view.auto_stop_max_duration', { max_duration_min: 90 })
     */
    window.t = function (key, subs) {
        if (!key) return undefined;
        var val = key.split('.').reduce(function (obj, part) {
            return (obj != null && obj[part] !== undefined) ? obj[part] : undefined;
        }, translations);
        if (val !== undefined && subs) {
            Object.keys(subs).forEach(function (k) {
                val = val.replace(new RegExp('\\{' + k + '\\}', 'g'), subs[k]);
            });
        }
        return val;
    };

    /**
     * Apply all [data-i18n*] translations within root (default: document).
     * Call again after dynamically injecting new HTML.
     */
    window.applyI18n = function (root) {
        root = root || document;

        root.querySelectorAll('[data-i18n]').forEach(function (el) {
            var v = window.t(el.getAttribute('data-i18n'));
            if (v !== undefined) el.textContent = v;
        });
        root.querySelectorAll('[data-i18n-html]').forEach(function (el) {
            var v = window.t(el.getAttribute('data-i18n-html'));
            if (v !== undefined) el.innerHTML = v;
        });
        root.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
            var v = window.t(el.getAttribute('data-i18n-placeholder'));
            if (v !== undefined) el.placeholder = v;
        });
        root.querySelectorAll('[data-i18n-title]').forEach(function (el) {
            var v = window.t(el.getAttribute('data-i18n-title'));
            if (v !== undefined) el.title = v;
        });
    };

    // Apply as soon as the DOM is ready
    document.addEventListener('DOMContentLoaded', function () {
        window.applyI18n();
    });

    /**
     * Switch to a different language, persist choice, and reload the page.
     */
    window.setLanguage = function (newLang) {
        localStorage.setItem('camui_lang', newLang);
        location.reload();
    };
}());
