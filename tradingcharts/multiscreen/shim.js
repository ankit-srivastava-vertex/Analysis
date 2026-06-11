/* multiscreen/shim.js
 * Loaded inside the workspace HTML BEFORE any inline script.
 * Namespaces every localStorage key with "tc:<WSID>:" by patching
 * Storage.prototype methods. The window.localStorage object itself
 * is left untouched so that identity checks (this === localStorage)
 * inside the page's own code keep working.
 */
(function () {
    var WSID = (typeof window !== 'undefined' && window.WSID) ? window.WSID : 'default';
    var PREFIX = 'tc:' + WSID + ':';

    if (!window.localStorage || !window.Storage || !Storage.prototype) return;

    var pk = function (k) {
        if (k === null || k === undefined) return k;
        var s = String(k);
        return s.indexOf(PREFIX) === 0 ? s : PREFIX + s;
    };

    var origSet = Storage.prototype.setItem;
    var origGet = Storage.prototype.getItem;
    var origRm = Storage.prototype.removeItem;
    var origKey = Storage.prototype.key;
    var origClear = Storage.prototype.clear;
    var lengthDesc = Object.getOwnPropertyDescriptor(Storage.prototype, 'length');
    var origLengthGet = lengthDesc ? lengthDesc.get : null;

    Storage.prototype.setItem = function (k, v) {
        var key = (this === window.localStorage) ? pk(k) : k;
        return origSet.call(this, key, v);
    };
    Storage.prototype.getItem = function (k) {
        var key = (this === window.localStorage) ? pk(k) : k;
        return origGet.call(this, key);
    };
    Storage.prototype.removeItem = function (k) {
        var key = (this === window.localStorage) ? pk(k) : k;
        return origRm.call(this, key);
    };

    Storage.prototype.key = function (i) {
        if (this !== window.localStorage) return origKey.call(this, i);
        var total = origLengthGet ? origLengthGet.call(this) : 0;
        var idx = 0;
        for (var j = 0; j < total; j++) {
            var rk = origKey.call(this, j);
            if (rk && rk.indexOf(PREFIX) === 0) {
                if (idx === i) return rk.slice(PREFIX.length);
                idx++;
            }
        }
        return null;
    };

    Storage.prototype.clear = function () {
        if (this !== window.localStorage) return origClear.call(this);
        var total = origLengthGet ? origLengthGet.call(this) : 0;
        var rm = [];
        for (var j = 0; j < total; j++) {
            var rk = origKey.call(this, j);
            if (rk && rk.indexOf(PREFIX) === 0) rm.push(rk);
        }
        for (var i = 0; i < rm.length; i++) origRm.call(this, rm[i]);
    };

    if (origLengthGet) {
        try {
            Object.defineProperty(Storage.prototype, 'length', {
                configurable: true,
                enumerable: lengthDesc.enumerable === true,
                get: function () {
                    var total = origLengthGet.call(this);
                    if (this !== window.localStorage) return total;
                    var n = 0;
                    for (var j = 0; j < total; j++) {
                        var rk = origKey.call(this, j);
                        if (rk && rk.indexOf(PREFIX) === 0) n++;
                    }
                    return n;
                },
            });
        } catch (e) { /* ignore */ }
    }

    // Belt-and-braces: ensure /api/state calls carry ?wsid=<id>, regardless of
    // which transport the page uses (fetch, XHR, or sendBeacon on unload).
    var addWsid = function (url) {
        var u = String(url || '');
        if (u.indexOf('/api/state') !== 0 || u.indexOf('wsid=') !== -1) return u;
        var sep = u.indexOf('?') === -1 ? '?' : '&';
        return u + sep + 'wsid=' + encodeURIComponent(WSID);
    };

    var _fetch = window.fetch;
    if (typeof _fetch === 'function') {
        window.fetch = function (input, init) {
            try {
                var url = (typeof input === 'string') ? input : (input && input.url) || '';
                var newUrl = addWsid(url);
                if (newUrl !== url) {
                    if (typeof input === 'string') input = newUrl;
                    else input = new Request(newUrl, input);
                }
            } catch (e) { /* noop */ }
            return _fetch.call(this, input, init);
        };
    }

    // The page's boot hydration uses synchronous XMLHttpRequest, which the
    // fetch wrapper above does NOT intercept. Without this, /api/state on
    // page load returns the *default* workspace's state and overwrites the
    // current workspace's localStorage.
    var XHR = window.XMLHttpRequest;
    if (XHR && XHR.prototype && typeof XHR.prototype.open === 'function') {
        var origOpen = XHR.prototype.open;
        XHR.prototype.open = function (method, url) {
            try { arguments[1] = addWsid(url); } catch (e) { /* noop */ }
            return origOpen.apply(this, arguments);
        };
    }

    // Unload-time state flush uses navigator.sendBeacon, which also bypasses
    // the fetch wrapper. Patch it so beacons land on the correct workspace.
    if (window.navigator && typeof window.navigator.sendBeacon === 'function') {
        var origBeacon = window.navigator.sendBeacon.bind(window.navigator);
        window.navigator.sendBeacon = function (url, data) {
            try { url = addWsid(url); } catch (e) { /* noop */ }
            return origBeacon(url, data);
        };
    }

    // Skip no-op setItem writes. The page's hydration writes server state into
    // localStorage, then setChartCount()/savePaneConfigs() rewrites those same
    // values during init. Without this skip, every reload re-fires schedulePush
    // and POSTs the just-hydrated state back to the server, creating a race
    // window where a delayed beacon from a previous reload (or a stale write
    // from another tab) can overwrite fresh state. Suppressing no-op writes
    // means init becomes truly idempotent — only real changes push.
    //
    // Installed after DOMContentLoaded so it sits OUTSIDE the page's own
    // setItem wrapper (which is installed during head-script execution).
    function installNoOpSkip() {
        var inner = Storage.prototype.setItem;
        Storage.prototype.setItem = function (k, v) {
            if (this === window.localStorage) {
                try {
                    var existing = Storage.prototype.getItem.call(this, k);
                    if (existing === String(v)) return;
                } catch (e) { /* fall through */ }
            }
            return inner.apply(this, arguments);
        };
    }
    if (document.readyState !== 'loading') {
        installNoOpSkip();
    } else {
        document.addEventListener('DOMContentLoaded', installNoOpSkip);
    }
})();
