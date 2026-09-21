/**
 * Frontend configuration.
 *
 * The backend lives on its own origin and auth is an HTTP-only cookie, so every
 * request goes out with credentials.
 *
 * Where that origin comes from depends on how the app is running:
 *
 * 1. `window.__F7OWS_API_BASE__` — the desktop build. The Tauri shell binds the
 *    Python sidecar to a free port (a fixed one would collide with whatever else
 *    the user has running) and injects the resulting URL before the bundle boots.
 *    The port is only known at launch, so it cannot be baked in at build time —
 *    which is why this is resolved at call time rather than at module load: the
 *    injection must be allowed to land first.
 * 2. `VITE_API_BASE_URL` — build-time override, still used by the cloud build.
 * 3. `http://localhost:8000` — the dev server default.
 */

/** @type {string | undefined} */
let cached;

/** The backend origin, without a trailing slash. */
export function apiBase() {
	if (cached === undefined) {
		const injected =
			typeof window !== 'undefined' ? /** @type {any} */ (window).__F7OWS_API_BASE__ : undefined;
		const value = injected || import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
		cached = String(value).replace(/\/$/, '');
	}
	return cached;
}
