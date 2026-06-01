<script>
	import { goto } from '$app/navigation';
	import { listProjects, createProject } from '$lib/api/projects.js';
	import { session } from '$lib/stores/session.svelte.js';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/** @type {import('$lib/api/projects.js').Project[]} */
	let projects = $state([]);
	let loading = $state(true);
	let creating = $state(false);
	let newName = $state('');
	/** @type {string|null} */
	let error = $state(null);

	$effect(() => {
		if (!session.authenticated) return;
		(async () => {
			try {
				projects = await listProjects();
			} catch (/** @type {any} */ err) {
				error = err?.message ?? 'Failed to load notebooks';
			} finally {
				loading = false;
			}
		})();
	});

	async function create() {
		const name = newName.trim();
		if (!name || creating) return;
		creating = true;
		error = null;
		try {
			const project = await createProject(name);
			goto(`/projects/${project.id}`);
		} catch (/** @type {any} */ err) {
			error = err?.message ?? 'Failed to create notebook';
			creating = false;
		}
	}
</script>

<div class="mx-auto max-w-2xl px-6 py-12">
	<div class="mb-8 flex items-baseline justify-between">
		<h1 class="text-[32px]">Your notebooks</h1>
		<span class="text-sm text-paper/50">{session.user?.email}</span>
	</div>

	<form
		class="mb-8 flex gap-2"
		onsubmit={(e) => {
			e.preventDefault();
			create();
		}}
	>
		<input
			bind:value={newName}
			placeholder="New notebook name…"
			class="flex-1 border border-paper bg-ink px-3 py-2 focus:outline-none"
		/>
		<Button type="submit" disabled={creating || !newName.trim()}>
			{creating ? 'Creating…' : 'Create'}
		</Button>
	</form>

	{#if error}<p class="mb-4 text-sm text-red-400">{error}</p>{/if}

	{#if loading}
		<Spinner label="loading…" />
	{:else if projects.length === 0}
		<p class="text-paper/40">No notebooks yet. Create one above to get started.</p>
	{:else}
		<ul class="flex flex-col">
			{#each projects as p (p.id)}
				<li>
					<a
						href={`/projects/${p.id}`}
						class="flex items-center justify-between border-b border-paper py-3 hover:text-accent"
					>
						<span class="text-[20px]">{p.name}</span>
						{#if p.description}<span class="text-sm text-paper/50">{p.description}</span>{/if}
					</a>
				</li>
			{/each}
		</ul>
	{/if}
</div>
