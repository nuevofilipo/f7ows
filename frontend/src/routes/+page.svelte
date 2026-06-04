<script>
	import { goto } from '$app/navigation';
	import { listProjects, createProject } from '$lib/api/projects.js';
	import { session } from '$lib/stores/session.svelte.js';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/** localStorage key for the most recently opened project (written by the notebook page). */
	const LAST_PROJECT_KEY = 'notebook:lastProject';

	// There is no notebook list anymore: the base domain just drops you into a
	// project. Prefer the most recently used one (remembered in localStorage),
	// else the newest. A brand-new user with zero projects gets one created in the
	// background and lands in it behind a forced onboarding modal (?onboarding=1).
	/** @type {string|null} */
	let error = $state(null);

	$effect(() => {
		if (!session.authenticated) return;
		(async () => {
			try {
				const projects = await listProjects();
				if (projects.length === 0) {
					const project = await createProject('Untitled notebook');
					goto(`/projects/${project.id}?onboarding=1`, { replaceState: true });
					return;
				}
				// Reopen the last-used project if it still exists, else the newest
				// (listProjects returns created_at DESC, so [0] is newest).
				const remembered = localStorage.getItem(LAST_PROJECT_KEY);
				const exists = remembered && projects.some((p) => p.id === remembered);
				const target = (exists && remembered) || projects[0].id;
				goto(`/projects/${target}`, { replaceState: true });
			} catch (/** @type {any} */ err) {
				error = err?.message ?? 'Failed to open your notebook';
			}
		})();
	});
</script>

{#if error}
	<div class="flex h-screen items-center justify-center px-6">
		<p class="text-sm text-red-400">{error}</p>
	</div>
{:else}
	<div class="flex h-screen items-center justify-center">
		<Spinner size={28} label="opening notebook…" />
	</div>
{/if}
