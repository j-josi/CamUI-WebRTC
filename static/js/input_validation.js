/**
 * Shared frontend input validation (multi-stage control, stages 1–3 and 5).
 *
 *  1. Keystroke filter  – only characters that can be part of a valid value
 *                         can be typed/pasted into an input. The number of
 *                         characters is limited to what the value range allows
 *                         (e.g. -5000…0 → at most 5 characters). For ranges that
 *                         only allow values <= 0 a "-" is prepended automatically
 *                         to every non-zero number.
 *  2. Inline message    – an invalid value shows
 *                         "Value must be in range between X and Y <unit>"
 *                         (or a matching "Value must …" sentence) directly
 *                         below the input; the input gets Bootstrap's is-invalid.
 *  3. Save-button guard – a button with data-iv-guard="<css selector>" is only
 *                         enabled while every active input inside that
 *                         container is valid; while disabled a hint above it
 *                         explains why (validation.save_blocked).
 *  5. Server errors     – InputValidation.showError(input, msg) shows a
 *                         backend rejection under the input.
 *  (Stage 4, the backend check, lives in validation.py.)
 *
 * Declarative configuration via data attributes on the <input>:
 *   data-iv="int|decimal|text"
 *   data-iv-min / data-iv-max   numeric range (falls back to the min/max attributes)
 *   data-iv-unit                unit shown in the range message (e.g. "ms")
 *   data-iv-required="true"     text only: empty is invalid (numbers are always required)
 *   data-iv-maxlen              text only: maximum length (also enforced while typing)
 *   data-iv-forbidden           text only: string of forbidden characters
 *   data-iv-no-leading          text only: string of characters the value must not start with
 *   data-iv-no-edge-space="true" text only: no leading / trailing whitespace
 *   data-iv-unique-group        text only: values must be unique (case-insensitive)
 *                               within all inputs sharing this group name
 *                               (an empty value falls back to the placeholder)
 *   data-iv-feedback            id of an existing element used for the message
 *                               (otherwise a div is created below the input)
 *
 * Messages are translatable via the "validation.*" i18n keys.
 */
(function () {
    'use strict';

    var CONTROL_CHARS = /[\x00-\x1f\x7f]/;

    function tr(key, subs, fallback) {
        var v = window.t ? window.t(key, subs) : undefined;
        if (v !== undefined) return v;
        if (subs) Object.keys(subs).forEach(function (k) { fallback = fallback.replace('{' + k + '}', subs[k]); });
        return fallback;
    }

    function fmtNum(n) {
        return String(parseFloat(Number(n).toFixed(6)));
    }

    function cfg(input) {
        var d = input.dataset;
        var min = d.ivMin !== undefined ? d.ivMin : input.getAttribute('min');
        var max = d.ivMax !== undefined ? d.ivMax : input.getAttribute('max');
        return {
            kind: d.iv,
            min: min !== null && min !== '' ? parseFloat(min) : null,
            max: max !== null && max !== '' ? parseFloat(max) : null,
            unit: d.ivUnit || '',
            required: d.ivRequired === 'true',
            maxlen: d.ivMaxlen ? parseInt(d.ivMaxlen, 10) : null,
            forbidden: d.ivForbidden || '',
            noLeading: d.ivNoLeading || '',
            noEdgeSpace: d.ivNoEdgeSpace === 'true',
            group: d.ivUniqueGroup || ''
        };
    }

    // ── Messages ────────────────────────────────────────────────────────
    function msgRange(c) {
        var lo = c.min, hi = c.max;
        if (lo !== null && hi !== null) {
            return tr('validation.range',
                { min: fmtNum(lo), max: fmtNum(hi), unit: c.unit ? ' ' + c.unit : '' },
                'Value must be in range between {min} and {max}{unit}');
        }
        return c.kind === 'int'
            ? tr('validation.whole_number', null, 'Value must be a whole number')
            : tr('validation.number', null, 'Value must be a number');
    }

    function describeChars(chars) {
        return Array.from(chars).map(function (ch) {
            return ch === ' ' ? tr('validation.space', null, 'space') : ch;
        }).join(' ');
    }

    // ── Keystroke filter (stage 1) ──────────────────────────────────────
    function stepDecimals(input) {
        var st = input.getAttribute('step');
        if (!st || st === 'any') return null;
        var i = st.indexOf('.');
        return i === -1 ? 0 : st.length - i - 1;
    }

    /** Maximum number of characters a valid value can have, or null if unbounded. */
    function maxChars(input, c) {
        if (c.kind === 'text') return c.maxlen;
        if (c.min === null || c.max === null) return null;
        var intLen = Math.max(String(Math.floor(Math.abs(c.min))).length,
                              String(Math.floor(Math.abs(c.max))).length);
        var len = intLen + (c.min < 0 ? 1 : 0);
        if (c.kind === 'decimal') {
            var dec = stepDecimals(input);
            if (dec === null) return null;
            if (dec > 0) len += 1 + dec;
        }
        return len;
    }

    /** True if the range only allows values <= 0 (so a minus sign is always required). */
    function nonPositiveRange(c) {
        return (c.kind === 'int' || c.kind === 'decimal') &&
               c.min !== null && c.min < 0 && c.max !== null && c.max <= 0;
    }

    function partialPattern(c, isTextInput) {
        var neg = c.min !== null && c.min < 0;
        if (c.kind === 'int')     return neg ? /^-?\d*$/ : /^\d*$/;
        if (c.kind === 'decimal') return neg ? /^-?\d*\.?\d*$/ : /^\d*\.?\d*$/;
        return null;
    }

    function charAllowed(c, ch) {
        if (c.kind === 'int')     return /[0-9]/.test(ch) || (ch === '-' && c.min !== null && c.min < 0);
        if (c.kind === 'decimal') return /[0-9.]/.test(ch) || (ch === '-' && c.min !== null && c.min < 0);
        // text
        return !CONTROL_CHARS.test(ch) && c.forbidden.indexOf(ch) === -1;
    }

    function acceptInsertion(input, c, text) {
        for (var i = 0; i < text.length; i++) {
            if (!charAllowed(c, text[i])) return false;
        }
        var re = partialPattern(c);
        var limit = maxChars(input, c);
        if (!re) {
            // text: respect the maximum length (position-aware where possible)
            if (limit !== null && typeof input.selectionStart === 'number') {
                var tv = input.value;
                var tnext = tv.slice(0, input.selectionStart) + text + tv.slice(input.selectionEnd);
                return tnext.length <= limit;
            }
            return true;
        }
        // Position-aware check (e.g. "-" only as first character, one "." only)
        // is only possible for inputs that expose their selection (type=text).
        if (typeof input.selectionStart === 'number' && input.type !== 'number') {
            var v = input.value;
            var next = v.slice(0, input.selectionStart) + text + v.slice(input.selectionEnd);
            // A leading digit gets its "-" added automatically afterwards, so
            // count it already now to keep the limit exact.
            var counted = nonPositiveRange(c) && /^\d*\.?\d*$/.test(next) && parseFloat(next) !== 0 && next !== ''
                ? next.length + 1 : next.length;
            if (limit !== null && counted > limit) return false;
            return re.test(next);
        }
        return true; // type=number: character filter only; value check reports leftovers
    }

    /** Apply native maxLength so pasting / autofill is truncated by the browser too. */
    function applyMaxLength(input) {
        if (input.type === 'number') return;
        var limit = maxChars(input, cfg(input));
        if (limit !== null && limit > 0) input.maxLength = limit;
    }

    /**
     * Remove every character that cannot be part of a number. Needed because dead
     * keys / IME composition (e.g. "^", "`") insert text that beforeinput cannot cancel.
     */
    function sanitizeNumeric(input) {
        var c = cfg(input);
        if ((c.kind !== 'int' && c.kind !== 'decimal') || input.type === 'number') return;
        var re = partialPattern(c);
        if (re.test(input.value)) return;
        var neg = c.min !== null && c.min < 0;
        var out = '', dot = false, caret = input.selectionStart || 0, drop = 0;
        Array.from(input.value).forEach(function (ch, i) {
            var ok = /[0-9]/.test(ch) ||
                     (ch === '-' && neg && out === '') ||
                     (ch === '.' && c.kind === 'decimal' && !dot);
            if (ok) { out += ch; if (ch === '.') dot = true; }
            else if (i < caret) drop++;
        });
        input.value = out;
        try { input.setSelectionRange(caret - drop, caret - drop); } catch (_) {}
    }

    /** Prepend "-" to non-zero numbers when only values <= 0 are allowed. */
    function autoMinus(input) {
        var c = cfg(input);
        if (!nonPositiveRange(c) || input.type === 'number') return;
        var v = input.value;
        if (!/^\d*\.?\d*$/.test(v) || !/\d/.test(v) || parseFloat(v) === 0) return;
        // strip superfluous leading zeros ("05" -> "5")
        v = v.replace(/^0+(?=\d)/, '');
        var caretFromEnd = input.value.length - (input.selectionStart || 0);
        input.value = '-' + v;
        try { var pos = Math.max(0, input.value.length - caretFromEnd); input.setSelectionRange(pos, pos); } catch (_) {}
    }

    document.addEventListener('focusin', function (e) {
        if (e.target && e.target.dataset && e.target.dataset.iv) applyMaxLength(e.target);
    }, true);

    // Capture phase: normalise the value before any other input listener reads it.
    document.addEventListener('input', function (e) {
        var input = e.target;
        if (!input || !input.dataset || !input.dataset.iv) return;
        sanitizeNumeric(input);
        autoMinus(input);
    }, true);

    document.addEventListener('compositionend', function (e) {
        var input = e.target;
        if (!input || !input.dataset || !input.dataset.iv) return;
        sanitizeNumeric(input);
        autoMinus(input);
        input.dispatchEvent(new Event('input', { bubbles: true }));
    }, true);

    document.addEventListener('beforeinput', function (e) {
        if (e.target && e.target.dataset && e.target.dataset.iv) applyMaxLength(e.target);
        var input = e.target;
        if (!input || !input.dataset || !input.dataset.iv) return;
        var text = e.data;
        if (text == null && e.dataTransfer) text = e.dataTransfer.getData('text');
        if (!text) return; // deletions, undo, … are always fine
        if (!acceptInsertion(input, cfg(input), text)) e.preventDefault();
    }, true);

    // ── Validation (stage 2) ────────────────────────────────────────────
    function uniqueKey(input) {
        return (input.value || input.placeholder || '').trim().toLowerCase();
    }

    /** Return an error message for the input's current value, or null if valid. */
    function check(input) {
        if (input._ivServerError) return input._ivServerError;
        var c = cfg(input);
        var v = input.value;

        if (c.kind === 'int' || c.kind === 'decimal') {
            var re = c.kind === 'int' ? /^-?\d+$/ : /^-?(\d+\.?\d*|\.\d+)$/;
            if (!re.test(v)) return msgRange(c);
            var n = parseFloat(v);
            if (!isFinite(n)) return msgRange(c);
            if ((c.min !== null && n < c.min) || (c.max !== null && n > c.max)) return msgRange(c);
            return null;
        }

        // text
        if (!v) {
            if (c.required) return tr('validation.required', null, 'Value must not be empty');
        } else {
            if (c.maxlen !== null && v.length > c.maxlen)
                return tr('validation.max_length', { max: c.maxlen }, 'Value must not exceed {max} characters');
            var bad = Array.from(v).filter(function (ch) { return c.forbidden.indexOf(ch) !== -1; });
            if (bad.length)
                return tr('validation.forbidden_chars', { chars: describeChars(c.forbidden) },
                          'Value must not contain any of these characters: {chars}');
            if (CONTROL_CHARS.test(v))
                return tr('validation.control_chars', null, 'Value must not contain control characters');
            if (c.noLeading && c.noLeading.indexOf(v[0]) !== -1)
                return tr('validation.no_leading', { chars: describeChars(c.noLeading) },
                          'Value must not start with {chars}');
            if (c.noEdgeSpace && v !== v.trim())
                return tr('validation.no_edge_space', null, 'Value must not start or end with a space');
        }
        if (c.group) {
            var key = uniqueKey(input);
            var dup = Array.from(document.querySelectorAll('[data-iv-unique-group="' + c.group + '"]'))
                .some(function (o) { return o !== input && !o.disabled && uniqueKey(o) === key; });
            if (dup) return tr('validation.unique', null, 'Value must be unique');
        }
        return null;
    }

    function feedbackEl(input) {
        var id = input.dataset.ivFeedback;
        var el = id ? document.getElementById(id) : null;
        if (el) {
            if (el._ivKeepSpace === undefined) el._ivKeepSpace = el.style.visibility !== '';
            return el;
        }
        if (!input._ivFeedback) {
            el = document.createElement('div');
            el.className = 'invalid-feedback small';
            el.style.fontSize = '0.75rem';
            el.style.display = 'none';
            (input.closest('.input-group') || input).insertAdjacentElement('afterend', el);
            input._ivFeedback = el;
        }
        return input._ivFeedback;
    }

    function render(input, message) {
        input.classList.toggle('is-invalid', !!message);
        var el = feedbackEl(input);
        if (message) el.textContent = message;
        if (el._ivKeepSpace) el.style.visibility = message ? 'visible' : 'hidden';
        else el.style.display = message ? 'block' : 'none';
    }

    /** Validate and render. Returns true if valid. Disabled inputs are treated as valid. */
    function validate(input) {
        var message = input.disabled ? null : check(input);
        render(input, message);
        input.dispatchEvent(new CustomEvent('iv:change', { bubbles: true, detail: { valid: !message } }));
        return !message;
    }

    function validateAll(container) {
        var ok = true;
        (container || document).querySelectorAll('[data-iv]').forEach(function (i) {
            if (!validate(i)) ok = false;
        });
        return ok;
    }

    function allValid(container) {
        return Array.from((container || document).querySelectorAll('[data-iv]'))
            .every(function (i) { return i.disabled || !check(i); });
    }

    // ── Save-button guard (stage 3) ─────────────────────────────────────
    /** Hint directly above a guarded button, shown while the button is disabled. */
    function guardHint(btn) {
        var hint = btn._ivHint;
        if (!hint) {
            hint = document.createElement('div');
            hint.className = 'small text-danger mb-1 w-100';
            hint.setAttribute('role', 'alert');
            hint.hidden = true;
            btn.insertAdjacentElement('beforebegin', hint);
            btn._ivHint = hint;
        }
        hint.textContent = tr('validation.save_blocked', null,
            'Correct the highlighted fields to save.');
        return hint;
    }

    function refreshGuards() {
        document.querySelectorAll('[data-iv-guard]').forEach(function (btn) {
            var scope = document.querySelector(btn.dataset.ivGuard);
            btn.disabled = scope ? !allValid(scope) : false;
            guardHint(btn).hidden = !btn.disabled || !scope;
        });
    }

    // ── Backend rejections (stage 5) ────────────────────────────────────
    function showError(input, message) {
        if (!input) return;
        input._ivServerError = message;
        render(input, message);
        refreshGuards();
    }

    function clearError(input) {
        if (input) input._ivServerError = null;
    }

    /** Drop any message / invalid state without validating (e.g. when a dialog is reopened). */
    function reset(input) {
        if (!input) return;
        input._ivServerError = null;
        render(input, null);
        refreshGuards();
    }

    document.addEventListener('input', function (e) {
        var input = e.target;
        if (!input || !input.dataset || !input.dataset.iv) return;
        clearError(input);
        validate(input);
        // Uniqueness depends on the other inputs of the group
        if (input.dataset.ivUniqueGroup) {
            document.querySelectorAll('[data-iv-unique-group="' + input.dataset.ivUniqueGroup + '"]')
                .forEach(function (o) { if (o !== input) validate(o); });
        }
    });

    document.addEventListener('iv:change', refreshGuards);
    document.addEventListener('DOMContentLoaded', refreshGuards);

    window.InputValidation = {
        check: check,
        validate: validate,
        validateAll: validateAll,
        allValid: allValid,
        refreshGuards: refreshGuards,
        showError: showError,
        clearError: clearError,
        reset: reset,
        msgRange: function (min, max, unit) {
            return msgRange({ kind: 'decimal', min: min, max: max, unit: unit || '' });
        }
    };
})();
