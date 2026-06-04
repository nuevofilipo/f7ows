/**
 * Accent-colour theming.
 *
 * The whole UI keys off the `--color-accent` CSS variable (see layout.css). The
 * user's chosen accent is the source of truth in their backend settings, but we
 * also mirror it to localStorage so it can be applied instantly on boot — before
 * the settings request resolves — avoiding a flash of the default pink.
 */

const STORAGE_KEY = 'accent_color';
export const DEFAULT_ACCENT = '#ed4bc7';

/** The preset swatches offered in the picker (from the Figma). */
export const ACCENT_PRESETS = ['#ed4bc7', '#f5a623', '#a8d4f0'];

/** @param {string} hex */
function isHex(hex) {
	return typeof hex === 'string' && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(hex);
}

/** Apply an accent colour to the document root (no-op on the server). @param {string} hex */
export function applyAccent(hex) {
	if (typeof document === 'undefined' || !isHex(hex)) return;
	document.documentElement.style.setProperty('--color-accent', hex);
}

/** Apply + persist the chosen accent. @param {string} hex */
export function setAccent(hex) {
	if (!isHex(hex)) return;
	applyAccent(hex);
	try {
		localStorage.setItem(STORAGE_KEY, hex);
	} catch {
		// localStorage may be unavailable (private mode) — theming still applies live.
	}
}

/** Apply the last-known accent from localStorage on boot, so there's no flash. */
export function initTheme() {
	if (typeof localStorage === 'undefined') return;
	const stored = localStorage.getItem(STORAGE_KEY);
	if (stored) applyAccent(stored);
}
