import adapter from '@sveltejs/adapter-static';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
		runes: ({ filename }) => (filename.split(/[/\\]/).includes('node_modules') ? undefined : true)
	},
	kit: {
		// Desktop build: there is no Node server to deploy to, so the frontend is
		// emitted as plain files that the Tauri shell serves from disk.
		//
		// Every route runs client-side already (`ssr = false` in +layout.js — auth
		// is a cookie for a separate backend origin, which the SvelteKit server
		// could never see), so nothing here is prerenderable and every URL has to
		// resolve to the same shell. That is what `fallback` is.
		//
		// index.html, not the 200.html the docs prefer: their reason to avoid it is
		// a collision with a prerendered homepage, and we prerender nothing. Tauri
		// boots its webview at the directory root, so the entry point has to be
		// index.html.
		adapter: adapter({ fallback: 'index.html' })
	}
};

export default config;
