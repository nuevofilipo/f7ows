/**
 * The app talks to a separate backend origin and auth is a browser-only cookie,
 * so the SvelteKit server can't see the session. Run as a client-side SPA.
 */
export const ssr = false;
export const prerender = false;
