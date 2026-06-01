/**
 * Frontend configuration.
 *
 * The backend lives on its own origin (default http://localhost:8000) and auth
 * is an HTTP-only cookie, so every request goes out with credentials. Override
 * the base URL with VITE_API_BASE_URL (e.g. on Railway) at build time.
 */
export const API_BASE =
	import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, '') ?? 'http://localhost:8000';
