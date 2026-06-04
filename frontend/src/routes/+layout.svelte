<script>
	import './layout.css';
	import favicon from '$lib/assets/favicon.svg';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { session } from '$lib/stores/session.svelte.js';
	import { initTheme } from '$lib/utils/theme.js';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	let { children } = $props();

	// Load the session once on boot, and apply the saved accent colour immediately
	// (from localStorage) so there's no flash of the default before settings load.
	$effect(() => {
		initTheme();
		session.init();
	});

	// Central auth guard: bounce unauthenticated users to /login, and logged-in
	// users away from /login.
	$effect(() => {
		if (session.loading) return;
		const onLogin = page.url.pathname === '/login';
		if (!session.authenticated && !onLogin) goto('/login');
		else if (session.authenticated && onLogin) goto('/');
	});
</script>

<svelte:head><link rel="icon" href={favicon} /></svelte:head>

{#if session.loading}
	<div class="flex h-screen items-center justify-center">
		<Spinner size={28} label="loading…" />
	</div>
{:else}
	{@render children()}
{/if}
