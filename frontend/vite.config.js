import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [tailwindcss(), sveltekit()],
	// Pin the dev port so it always matches the backend's CORS_ORIGINS
	// (default http://localhost:5173). strictPort makes Vite fail loudly if the
	// port is taken instead of silently drifting to 5174 (which breaks CORS).
	server: { port: 5173, strictPort: true }
});
