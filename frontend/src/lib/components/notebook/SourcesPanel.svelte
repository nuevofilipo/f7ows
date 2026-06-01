<script>
	import { listDocuments, uploadDocuments } from '$lib/api/documents.js';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/**
	 * Left column — PDF sources. Upload kicks off the backend ingest pipeline;
	 * we poll the list while anything is still processing.
	 * @type {{ projectId: string }}
	 */
	let { projectId } = $props();

	/** @type {import('$lib/api/documents.js').Document[]} */
	let docs = $state([]);
	let uploading = $state(false);
	/** @type {string|null} */
	let error = $state(null);
	/** @type {HTMLInputElement} */
	let fileInput;

	const READY = new Set(['ready', 'error']);
	const pending = $derived(docs.some((d) => !READY.has(d.status?.state ?? '')));

	async function refresh() {
		try {
			docs = await listDocuments(projectId);
		} catch (/** @type {any} */ err) {
			error = err?.message ?? 'Failed to load sources';
		}
	}

	// Poll while any document is still ingesting.
	$effect(() => {
		refresh();
	});
	$effect(() => {
		if (!pending) return;
		const id = setInterval(refresh, 2500);
		return () => clearInterval(id);
	});

	/** @param {Event} e */
	async function onPick(e) {
		const input = /** @type {HTMLInputElement} */ (e.target);
		const files = Array.from(input.files ?? []);
		if (!files.length) return;
		uploading = true;
		error = null;
		try {
			const res = await uploadDocuments(projectId, files);
			if (res.errors?.length) error = res.errors.map((x) => `${x.filename}: ${x.error}`).join('; ');
			await refresh();
		} catch (/** @type {any} */ err) {
			error = err?.message ?? 'Upload failed';
		} finally {
			uploading = false;
			input.value = '';
		}
	}

	/** @param {import('$lib/api/documents.js').Document} d */
	function stateLabel(d) {
		const s = d.status?.state ?? 'uploaded';
		return s === 'ready' ? '' : s;
	}
</script>

<section class="flex h-full min-h-0 flex-col border border-paper">
	<header class="flex items-center justify-between gap-2 px-4 py-3">
		<h2 class="text-[22px]">Sources</h2>
		<button
			class="bg-accent px-4 py-2 text-[15px] text-paper hover:opacity-90 disabled:opacity-40"
			disabled={uploading}
			onclick={() => fileInput.click()}
		>
			{uploading ? 'Uploading…' : 'Add sources'}
		</button>
		<input bind:this={fileInput} type="file" accept="application/pdf" multiple class="hidden" onchange={onPick} />
	</header>

	<div class="scroll-clean min-h-0 flex-1 overflow-y-auto px-4 py-3">
		{#if docs.length === 0}
			<p class="text-sm text-paper/40">No sources yet. Add a PDF to get started.</p>
		{:else}
			<ul class="flex flex-col gap-2">
				{#each docs as d (d.id)}
					<li class="flex items-center justify-between gap-2 text-[15px]">
						<span class="truncate" title={d.name}>{d.name}</span>
						{#if stateLabel(d)}
							<span class="shrink-0 text-xs text-paper/50">
								{#if stateLabel(d) !== 'error'}<Spinner size={10} />{/if}
								{stateLabel(d)}
							</span>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
		{#if error}<p class="mt-2 text-xs text-red-400">{error}</p>{/if}
	</div>
</section>
