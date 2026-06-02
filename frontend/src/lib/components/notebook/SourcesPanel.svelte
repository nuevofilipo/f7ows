<script>
  import { listDocuments, uploadDocuments } from "$lib/api/documents.js";
  import { sources } from "$lib/stores/sources.svelte.js";
  import Spinner from "$lib/components/ui/Spinner.svelte";

  /**
   * Left column — PDF sources. Upload kicks off the backend ingest pipeline;
   * we poll the list while anything is still processing. Each ready source has a
   * checkbox; the selection (kept in the `sources` store) scopes what the chat
   * retrieves against.
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

  const READY = new Set(["ready", "error"]);
  const pending = $derived(docs.some((d) => !READY.has(d.status?.state ?? "")));
  /** @param {import('$lib/api/documents.js').Document} d */
  const isReady = (d) => (d.status?.state ?? "") === "ready";
  // Only ready documents can be selected (nothing else has embeddings to retrieve).
  const readyDocs = $derived(docs.filter(isReady));
  // Drives the master checkbox: checked only when every ready doc is selected.
  const allSelected = $derived(readyDocs.length > 0 && readyDocs.every((d) => sources.selected.has(d.id)));

  async function refresh() {
    try {
      docs = await listDocuments(projectId);
      // Keep the selection in step: auto-select newly-ready docs, prune gone ones.
      sources.sync(
        projectId,
        docs.map((d) => ({ id: d.id, ready: isReady(d) })),
      );
    } catch (/** @type {any} */ err) {
      error = err?.message ?? "Failed to load sources";
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
      if (res.errors?.length) error = res.errors.map((x) => `${x.filename}: ${x.error}`).join("; ");
      await refresh();
    } catch (/** @type {any} */ err) {
      error = err?.message ?? "Upload failed";
    } finally {
      uploading = false;
      input.value = "";
    }
  }

  /** @param {import('$lib/api/documents.js').Document} d */
  function stateLabel(d) {
    const s = d.status?.state ?? "uploaded";
    return s === "ready" ? "" : s;
  }
</script>

<section class="flex h-full min-h-0 flex-col border border-paper">
  <header class="flex items-center justify-between gap-2 px-4 py-3">
    <h2 class="text-[20px]">Sources</h2>
    <button
      class="bg-accent px-3 py-1 text-[16px] text-paper hover:opacity-90 disabled:opacity-40"
      disabled={uploading}
      onclick={() => fileInput.click()}
    >
      {uploading ? "Uploading…" : "Add sources"}
    </button>
    <input bind:this={fileInput} type="file" accept="application/pdf" multiple class="hidden" onchange={onPick} />
  </header>

  <!-- Rectangular checkbox: same border as everywhere; when selected a centred
	     pink square fills it. ready=false dims the empty box. -->
  {#snippet box(/** @type {boolean} */ selected, /** @type {boolean} */ ready = true)}
    <span class="flex h-4 w-4 shrink-0 items-center justify-center border border-paper {ready ? '' : 'opacity-30'}">
      {#if selected}<span class="h-2.5 w-2.5 bg-accent"></span>{/if}
    </span>
  {/snippet}

  <div class="scroll-clean min-h-0 flex-1 overflow-y-auto px-4 py-3">
    {#if docs.length === 0}
      <p class="text-sm text-paper/40">No sources yet. Add a PDF to get started.</p>
    {:else}
      <ul class="flex flex-col gap-2">
        <!-- Master row: no label, just the box on the right — toggles all sources. -->
        {#if readyDocs.length > 1}
          <li>
            <button
              type="button"
              class="flex w-full items-center justify-end"
              aria-label={allSelected ? "Deselect all sources" : "Select all sources"}
              aria-pressed={allSelected}
              onclick={() => (allSelected ? sources.clear() : sources.selectAll(readyDocs.map((d) => d.id)))}
            >
              {@render box(allSelected)}
            </button>
          </li>
        {/if}

        {#each docs as d (d.id)}
          {@const ready = isReady(d)}
          {@const selected = sources.selected.has(d.id)}
          <li class="flex items-center justify-between gap-3 text-[15px]">
            <span class="min-w-0 flex-1 truncate {ready ? '' : 'text-paper/50'}" title={d.name}>
              {d.name}
            </span>
            {#if stateLabel(d)}
              <span class="flex shrink-0 items-center gap-1 text-xs text-paper/50">
                {#if stateLabel(d) !== "error"}<Spinner size={10} />{/if}
                {stateLabel(d)}
              </span>
            {/if}
            <!-- Only the box toggles selection; clicking the label does nothing. -->
            <button
              type="button"
              disabled={!ready}
              aria-pressed={selected}
              aria-label={selected ? `Deselect ${d.name}` : `Select ${d.name}`}
              class="disabled:cursor-default"
              onclick={() => sources.toggle(d.id)}
            >
              {@render box(selected, ready)}
            </button>
          </li>
        {/each}
      </ul>
    {/if}
    {#if error}<p class="mt-2 text-xs text-red-400">{error}</p>{/if}
  </div>
</section>
