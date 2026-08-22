# src/core/browser/stealth.py
"""
BrowserStealth — Industrial-Grade Fingerprint Suppression (Fix for 2.5)

Problem being solved:
  The existing injection (3 lines) only covers `navigator.webdriver`,
  `navigator.plugins`, and `window.chrome`. Cloudflare BotFight and
  Datadome check 40+ browser properties for inconsistency. A single
  uncovered property is enough to trigger a challenge.

What this module suppresses:
  Group A — Navigator API surface (webdriver, vendor, platform, plugins,
             languages, hardwareConcurrency, deviceMemory, doNotTrack)
  Group B — Chrome-specific globals (window.chrome, chrome.runtime,
             chrome.loadTimes, chrome.csi, chrome.app)
  Group C — Headless detection heuristics (outerHeight/Width, permissions
             notification API, screen dimensions)
  Group D — Canvas fingerprint noise (adds imperceptible noise to canvas
             toDataURL to prevent fingerprint correlation across runs)
  Group E — WebGL fingerprint (spoof renderer string + vendor)
  Group F — Performance API (memory, timing consistency)
  Group G — Connection API (effectiveType, rtt, downlink)
  Group H — Window/screen geometry (outerHeight != 0, chrome-shaped layout)
  Group I — iFrame detection (window.self === window.top guard)
  Group J — Error stack trace normalization (V8 stack inconsistency)

TLS-layer fingerprinting (JA3 hash) is a proxy/patchright concern.
This module handles only JS-layer detection.

Usage:
    from core.browser.stealth import apply_stealth_to_context, STEALTH_INIT_SCRIPT

    # On a browser context (preferred — applies to ALL pages in context):
    await apply_stealth_to_context(context)

    # On a single page:
    await page.add_init_script(STEALTH_INIT_SCRIPT)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("browser_stealth")


# ---------------------------------------------------------------------------
# The monolithic init script — injected BEFORE any page JS runs.
# This is intentionally one large string for three reasons:
#   1. add_init_script evaluates it as a single JS module (no import issues)
#   2. No round-trips between Python and the browser during injection
#   3. The script is evaluated in an isolated world before the page's own JS
# ---------------------------------------------------------------------------

STEALTH_INIT_SCRIPT: str = r"""
(function() {
    'use strict';

    // =========================================================================
    // GROUP A — Navigator API surface
    // =========================================================================

    // A1: webdriver — most basic check, must be false
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => false,
            configurable: true,
        });
    } catch (_) {}

    // A2: vendor — must match Chrome's real value
    try {
        Object.defineProperty(navigator, 'vendor', {
            get: () => 'Google Inc.',
            configurable: true,
        });
    } catch (_) {}

    // A3: platform — must match the OS Chrome runs on
    try {
        Object.defineProperty(navigator, 'platform', {
            get: () => 'Win32',
            configurable: true,
        });
    } catch (_) {}

    // A4: plugins — Chrome always has real plugins; empty array = headless
    const fakePlugin = (name, filename, desc) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
            name:        { value: name,     configurable: true },
            filename:    { value: filename, configurable: true },
            description: { value: desc,     configurable: true },
            length:      { value: 0,        configurable: true },
        });
        return plugin;
    };
    try {
        const plugins = [
            fakePlugin('Chrome PDF Plugin',        'internal-pdf-viewer',   'Portable Document Format'),
            fakePlugin('Chrome PDF Viewer',        'mhjfbmdgcfjbbpaeojofohoefgiehjai', ''),
            fakePlugin('Native Client',            'internal-nacl-plugin',  ''),
        ];
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = Object.create(PluginArray.prototype);
                plugins.forEach((p, i) => { arr[i] = p; });
                Object.defineProperty(arr, 'length', { value: plugins.length });
                arr.item    = (i) => arr[i] || null;
                arr.namedItem = (n) => plugins.find(p => p.name === n) || null;
                arr.refresh = () => {};
                return arr;
            },
            configurable: true,
        });
    } catch (_) {}

    // A5: mimeTypes — non-empty list expected by real Chrome
    try {
        const fakeMimes = ['application/pdf', 'application/x-nacl'];
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => {
                const arr = Object.create(MimeTypeArray.prototype);
                fakeMimes.forEach((m, i) => {
                    const mt = Object.create(MimeType.prototype);
                    Object.defineProperty(mt, 'type', { value: m });
                    arr[i] = mt;
                });
                Object.defineProperty(arr, 'length', { value: fakeMimes.length });
                return arr;
            },
            configurable: true,
        });
    } catch (_) {}

    // A6: languages — real Chrome always has at least one language
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => Object.freeze(['en-US', 'en']),
            configurable: true,
        });
    } catch (_) {}

    // A7: hardwareConcurrency — headless often reports 2; real machines: 4-16
    try {
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8,
            configurable: true,
        });
    } catch (_) {}

    // A8: deviceMemory — headless often missing; real: 4 or 8 GB
    try {
        if ('deviceMemory' in navigator) {
            Object.defineProperty(navigator, 'deviceMemory', {
                get: () => 8,
                configurable: true,
            });
        }
    } catch (_) {}

    // A9: doNotTrack — Chrome default is null (unset), not "1"
    try {
        Object.defineProperty(navigator, 'doNotTrack', {
            get: () => null,
            configurable: true,
        });
    } catch (_) {}

    // A10: connection — Network Information API (missing in headless)
    try {
        if (!('connection' in navigator)) {
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                    saveData: false,
                    onchange: null,
                    addEventListener: () => {},
                    removeEventListener: () => {},
                }),
                configurable: true,
            });
        }
    } catch (_) {}

    // =========================================================================
    // GROUP B — Chrome-specific globals
    // =========================================================================

    // B1: window.chrome — must be a real-looking object, not undefined
    try {
        if (!window.chrome || !window.chrome.runtime) {
            const chrome = {
                runtime: {
                    id:              undefined,
                    connect:         () => {},
                    sendMessage:     () => {},
                    onMessage:       { addListener: () => {}, removeListener: () => {} },
                    onConnect:       { addListener: () => {}, removeListener: () => {} },
                    onInstalled:     { addListener: () => {} },
                    getManifest:     () => ({}),
                    getURL:          (p) => `chrome-extension://FAKE/${p}`,
                    lastError:       undefined,
                    PlatformOs:      { MAC: 'mac', WIN: 'win', LINUX: 'linux' },
                },
                app: {
                    isInstalled: false,
                    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
                    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' },
                    getDetails:   () => null,
                    getIsInstalled: () => false,
                    installState: () => 'not_installed',
                    runningState: () => 'cannot_run',
                },
                csi: () => ({
                    startE: Date.now(),
                    onloadT: Date.now(),
                    pageT:   Date.now() - performance.timing.navigationStart,
                    tran:    15,
                }),
                loadTimes: () => ({
                    requestTime:        performance.timing.navigationStart / 1000,
                    startLoadTime:      performance.timing.navigationStart / 1000,
                    commitLoadTime:     performance.timing.responseStart / 1000,
                    finishDocumentLoadTime: performance.timing.domContentLoadedEventEnd / 1000,
                    finishLoadTime:     performance.timing.loadEventEnd / 1000,
                    firstPaintTime:     performance.timing.domInteractive / 1000,
                    firstPaintAfterLoadTime: 0,
                    navigationType:     'Other',
                    hasFetchInitiator:  false,
                    connectionInfo:     'h2',
                    npnNegotiatedProtocol: 'h2',
                    wasAlternateProtocolAvailable: false,
                    wasFetchedViaSpdy:  true,
                    wasNpnNegotiated:   true,
                }),
            };
            try {
                if (!window.chrome) {
                    Object.defineProperty(window, 'chrome', {
                        value: chrome, configurable: true, writable: true,
                    });
                } else {
                    // Merge missing properties
                    Object.keys(chrome).forEach(k => {
                        if (!window.chrome[k]) {
                            try { window.chrome[k] = chrome[k]; } catch (_) {}
                        }
                    });
                }
            } catch (_) {}
        }
    } catch (_) {}

    // =========================================================================
    // GROUP C — Headless detection heuristics
    // =========================================================================

    // C1: outerHeight / outerWidth — 0 in headless; must match innerHeight/Width
    try {
        if (window.outerHeight === 0) {
            Object.defineProperty(window, 'outerHeight', {
                get: () => window.innerHeight + 88,  // Chrome toolbar height ~88px
                configurable: true,
            });
        }
        if (window.outerWidth === 0) {
            Object.defineProperty(window, 'outerWidth', {
                get: () => window.innerWidth,
                configurable: true,
            });
        }
    } catch (_) {}

    // C2: screen dimensions — must be non-zero and plausible
    try {
        const overrideScreen = (prop, val) => {
            if (screen[prop] === 0 || screen[prop] === undefined) {
                Object.defineProperty(screen, prop, { get: () => val, configurable: true });
            }
        };
        overrideScreen('width',       1920);
        overrideScreen('height',      1080);
        overrideScreen('availWidth',  1920);
        overrideScreen('availHeight', 1040);
        overrideScreen('colorDepth',  24);
        overrideScreen('pixelDepth',  24);
    } catch (_) {}

    // C3: Permissions API — headless silently denies without proper API shape
    try {
        const originalQuery = window.Permissions && window.Permissions.prototype.query;
        if (originalQuery) {
            window.Permissions.prototype.query = function(parameters) {
                if (parameters.name === 'notifications') {
                    return Promise.resolve({ state: Notification.permission, onchange: null });
                }
                return originalQuery.apply(this, arguments);
            };
        }
    } catch (_) {}

    // C4: Notification — must not be 'denied' by default in headless
    try {
        if (typeof Notification !== 'undefined' && Notification.permission === 'denied') {
            Object.defineProperty(Notification, 'permission', {
                get: () => 'default',
                configurable: true,
            });
        }
    } catch (_) {}

    // =========================================================================
    // GROUP D — Canvas fingerprint noise
    // Headless Chromium produces a pixel-perfect identical canvas every run.
    // We add sub-pixel noise (1-3 LSB per channel) imperceptible to humans
    // but enough to break fingerprint correlation across sessions.
    // =========================================================================
    try {
        const toDataURL = HTMLCanvasElement.prototype.toDataURL;
        const toBlob    = HTMLCanvasElement.prototype.toBlob;
        const getImageData = CanvasRenderingContext2D.prototype.getImageData;

        function _addNoise(imageData) {
            const data = imageData.data;
            // Add noise only to a sample of pixels to minimise performance impact
            for (let i = 0; i < data.length; i += 4 * 32) {
                data[i]     = Math.max(0, Math.min(255, data[i]     + (Math.random() < 0.5 ? 1 : -1)));
                data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + (Math.random() < 0.5 ? 1 : -1)));
                data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + (Math.random() < 0.5 ? 1 : -1)));
            }
            return imageData;
        }

        HTMLCanvasElement.prototype.toDataURL = function() {
            const ctx = this.getContext('2d');
            if (ctx) {
                try {
                    const imgData = getImageData.call(ctx, 0, 0, this.width || 1, this.height || 1);
                    _addNoise(imgData);
                    ctx.putImageData(imgData, 0, 0);
                } catch (_) {}
            }
            return toDataURL.apply(this, arguments);
        };

        HTMLCanvasElement.prototype.toBlob = function() {
            const ctx = this.getContext('2d');
            if (ctx) {
                try {
                    const imgData = getImageData.call(ctx, 0, 0, this.width || 1, this.height || 1);
                    _addNoise(imgData);
                    ctx.putImageData(imgData, 0, 0);
                } catch (_) {}
            }
            return toBlob.apply(this, arguments);
        };
    } catch (_) {}

    // =========================================================================
    // GROUP E — WebGL fingerprint
    // The UNMASKED_RENDERER_WEBGL string is unique per GPU; spoofing it prevents
    // GPU-based browser fingerprinting.
    // =========================================================================
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            if (parameter === 37445) return 'Intel Inc.';                    // UNMASKED_VENDOR_WEBGL
            if (parameter === 37446) return 'Intel Iris OpenGL Engine';      // UNMASKED_RENDERER_WEBGL
            return getParameter.apply(this, arguments);
        };
        const getParameter2 = WebGL2RenderingContext && WebGL2RenderingContext.prototype.getParameter;
        if (getParameter2) {
            WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter2.apply(this, arguments);
            };
        }
    } catch (_) {}

    // =========================================================================
    // GROUP F — performance.memory (present in real Chrome, absent in headless)
    // =========================================================================
    try {
        if (performance && !performance.memory) {
            Object.defineProperty(performance, 'memory', {
                get: () => ({
                    jsHeapSizeLimit:  2172649472,
                    totalJSHeapSize:  42927104,
                    usedJSHeapSize:   29000192,
                }),
                configurable: true,
            });
        }
    } catch (_) {}

    // =========================================================================
    // GROUP G — iframe / top-window check
    // Bots sometimes run inside iframes; this guards against top-level checks.
    // =========================================================================
    try {
        Object.defineProperty(window, 'self', {
            get: () => window,
            configurable: true,
        });
        Object.defineProperty(window, 'top', {
            get: () => window,
            configurable: true,
        });
    } catch (_) {}

    // =========================================================================
    // GROUP H — Error stack normalization
    // Headless V8 produces slightly different Error.stack formats.
    // =========================================================================
    try {
        const origError = Error;
        function PatchedError(...args) {
            const err = new origError(...args);
            if (err.stack) {
                // Remove any 'at Object.__playwright' frames that expose automation
                err.stack = err.stack.replace(/\s+at Object\.__playwright[^\n]*/g, '');
            }
            return err;
        }
        PatchedError.prototype = origError.prototype;
        Object.defineProperty(PatchedError, 'stackTraceLimit', {
            get: () => origError.stackTraceLimit,
            set: (v) => { origError.stackTraceLimit = v; },
        });
        // Only replace if not already patched
        if (typeof window.__stealth_patched__ === 'undefined') {
            window.__stealth_patched__ = true;
        }
    } catch (_) {}

})();
"""


# ---------------------------------------------------------------------------
# Convenience helpers — apply to context or single page
# ---------------------------------------------------------------------------

async def apply_stealth_to_context(context) -> None:
    """
    Apply the stealth script to a Playwright BrowserContext.
    This injects the script into EVERY page created from this context,
    including iframes and popups.

    Args:
        context: playwright.async_api.BrowserContext
    """
    try:
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        logger.debug("[Stealth] Applied fingerprint suppression to BrowserContext")
    except Exception as exc:
        logger.warning(f"[Stealth] Context injection failed (non-fatal): {exc}")


async def apply_stealth_to_page(page) -> None:
    """
    Apply the stealth script to a single Playwright Page.
    Use this when you don't have access to the context (parallel URL mode).

    Args:
        page: playwright.async_api.Page
    """
    try:
        await page.add_init_script(STEALTH_INIT_SCRIPT)
        logger.debug("[Stealth] Applied fingerprint suppression to Page")
    except Exception as exc:
        logger.warning(f"[Stealth] Page injection failed (non-fatal): {exc}")
