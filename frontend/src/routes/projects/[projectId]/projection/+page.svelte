<script>
  import { page } from "$app/state";
  import { goto } from "$app/navigation";
  import { session } from "$lib/stores/session.svelte.js";
  import { listProjects } from "$lib/api/projects.js";
  import { getProjection, searchProjection, resetStudyProgress } from "$lib/api/projection.js";
  import { getSettings, updateSettings } from "$lib/api/settings.js";
  import ProjectionMap from "$lib/components/projection/ProjectionMap.svelte";
  import Spinner from "$lib/components/ui/Spinner.svelte";
  import Modal from "$lib/components/ui/Modal.svelte";
  import { SvelteSet } from "svelte/reactivity";

  const projectId = $derived(page.params.projectId ?? "");

  let projectName = $state("Notebook");
  /** @type {import('$lib/api/projection.js').ProjectionChunk[]} */
  let chunks = $state([]);
  let ready = $state(false);
  let loading = $state(true);
  /** @type {string|null} */
  let error = $state(null);

  // Which reduction to plot. Single-select, like the design's checkboxes.
  /** @type {'umap_2d'|'pca'|'tsne'} */
  let method = $state("umap_2d");

  // Layout controls. `declutter` runs the non-overlap relaxation; `anchorStrength`
  // (0..1, medium by default) trades UMAP-shape fidelity against airier spacing.
  let declutter = $state(true);
  let anchorStrength = $state(0.5);
  const METHODS = /** @type {const} */ ([
    ["umap_2d", "UMAP Projection"],
    ["pca", "PCA Projection"],
    ["tsne", "t-SNE Projection"],
  ]);

  // Search state. `highlight` holds the ids the map should ring; `searchActive`
  // dims everything else once a search has run.
  let query = $state("");
  /** @type {'cosine'|'bm25'} */
  let searchMethod = $state("cosine");
  // How many top matches the search highlights (top-k, no relevance floor).
  let topK = $state(5);
  // SvelteSet is reactive on its own; mutate in place rather than reassigning.
  const highlight = new SvelteSet();
  let searchActive = $state(false);
  let searching = $state(false);

  // Persisted projection knobs live in the user's preferences (projection group).
  // `settingsReady` gates the auto-save effect until the stored values are applied,
  // and `lastSavedSnapshot` suppresses the redundant write of the just-loaded values.
  let settingsReady = $state(false);
  let lastSavedSnapshot = "";
  /** @type {ReturnType<typeof setTimeout>|undefined} */
  let saveTimer;

  $effect(() => {
    if (!session.authenticated || !projectId) return;
    loading = true;
    (async () => {
      try {
        // No "get one project" endpoint — find its name in the list.
        const [projects, projection, settings] = await Promise.all([listProjects(), getProjection(projectId), getSettings()]);
        projectName = projects.find((p) => p.id === projectId)?.name ?? "Notebook";
        chunks = projection.chunks;
        ready = projection.ready;

        // Apply stored projection preferences (fall back to the in-script defaults).
        const e = settings.effective ?? {};
        method = e.projection_method ?? method;
        declutter = e.projection_declutter ?? declutter;
        anchorStrength = e.projection_anchor_strength ?? anchorStrength;
        searchMethod = e.projection_search_method ?? searchMethod;
        topK = e.projection_search_k ?? topK;
        lastSavedSnapshot = projectionSnapshot();
        settingsReady = true;
      } catch (/** @type {any} */ err) {
        error = err?.message ?? "Failed to load projection";
      } finally {
        loading = false;
      }
    })();
  });

  /** The five persisted knobs as a settings patch (stable key order for diffing). */
  function projectionSnapshot() {
    return JSON.stringify({
      projection_method: method,
      projection_declutter: declutter,
      projection_anchor_strength: anchorStrength,
      projection_search_method: searchMethod,
      projection_search_k: topK,
    });
  }

  // Debounced auto-save: persist whenever a projection knob changes (no Save button).
  $effect(() => {
    const snapshot = projectionSnapshot();
    if (!settingsReady || snapshot === lastSavedSnapshot) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      lastSavedSnapshot = snapshot;
      updateSettings(JSON.parse(snapshot)).catch((/** @type {any} */ err) => {
        error = err?.message ?? "Failed to save settings";
      });
    }, 500);
  });

  async function runSearch() {
    const q = query.trim();
    if (!q) {
      highlight.clear();
      searchActive = false;
      return;
    }
    searching = true;
    try {
      const res = await searchProjection(projectId, q, searchMethod, topK);
      highlight.clear();
      for (const m of res.matches) highlight.add(m.id);
      searchActive = true;
    } catch (/** @type {any} */ err) {
      error = err?.message ?? "Search failed";
    } finally {
      searching = false;
    }
  }

  // Reset study progress: clears all actively/passively studied marks back to
  // not_studied (irreversible), then recolors the map locally without a refetch.
  // `confirmReset` gates the action behind a designed modal rather than a browser confirm.
  let resetting = $state(false);
  let confirmReset = $state(false);
  async function resetStudy() {
    resetting = true;
    try {
      await resetStudyProgress(projectId);
      chunks = chunks.map((c) => ({ ...c, study: "not_studied" }));
      confirmReset = false;
    } catch (/** @type {any} */ err) {
      error = err?.message ?? "Failed to reset study progress";
    } finally {
      resetting = false;
    }
  }

  async function logout() {
    await session.logout();
    goto("/login");
  }

  let profileOpen = $state(false);

  const LEGEND = /** @type {const} */ ([
    ["actively_studied", "actively studied"],
    ["passively_studied", "passively studied"],
    ["not_studied", "not studied"],
  ]);
</script>

<div class="flex h-screen flex-col bg-ink p-2 pt-0">
  <!-- Top bar — mirrors the notebook shell, with a link back to the notebook. -->
  <header class="flex items-center justify-between py-0">
    <h1 class="text-[20px]">Projection</h1>
    <nav class="flex items-center gap-6 text-[20px]">
      <a href={`/projects/${projectId}`} class="hover:text-accent">Notebook</a>
      <div class="relative">
        <button onclick={() => (profileOpen = !profileOpen)}>My Profile</button>
        {#if profileOpen}
          <div class="absolute right-0 z-50 mt-2 w-56 border border-paper bg-ink p-3 text-sm">
            <p class="truncate text-paper/70">{session.user?.email}</p>
            <a href="/profile" class="mt-2 block w-full border border-paper px-3 py-1 text-center text-paper hover:bg-paper/10">Settings</a>
            {#if !session.singleUser}
              <button class="mt-2 w-full bg-accent px-3 py-1 text-paper hover:opacity-90" onclick={logout}> Log out </button>
            {/if}
          </div>
        {/if}
      </div>
    </nav>
  </header>

  {#if loading}
    <div class="flex flex-1 items-center justify-center">
      <Spinner size={28} label="loading projection…" />
    </div>
  {:else if error}
    <div class="flex flex-1 items-center justify-center">
      <p class="text-red-400">{error}</p>
    </div>
  {:else}
    <!-- Map (left) + Settings sidebar (right), flush like the notebook panes. -->
    <div class="flex min-h-0 flex-1">
      <!-- Map canvas -->
      <section class="relative min-w-0 flex-1 border border-paper">
        {#if !ready}
          <div class="absolute inset-0 flex items-center justify-center">
            <p class="text-sm text-paper/40">No projection yet — add sources, then the map fills in once they finish processing.</p>
          </div>
        {:else}
          <ProjectionMap {projectId} {chunks} {method} {highlight} {searchActive} {declutter} {anchorStrength} />
        {/if}

        <!-- Legend, bottom-left over the canvas. -->
        <ul class="absolute bottom-3 left-3 flex flex-col gap-1.5 text-[15px]">
          {#each LEGEND as [key, label] (key)}
            <li class="flex items-center gap-2">
              <span class="flex h-4 w-4 items-center justify-center border {key === 'not_studied' ? 'border-paper' : 'border-accent'}">
                {#if key === "actively_studied"}<span class="h-full w-full bg-accent"></span>{/if}
              </span>
              {label}
            </li>
          {/each}
        </ul>
      </section>

      <!-- Settings sidebar -->
      <aside class="-ml-px flex w-[300px] shrink-0 flex-col border border-paper p-4">
        <h2 class="text-[18px]">Settings</h2>

        <!-- Projection method: single-select, app-style square boxes. -->
        <ul class="mt-3 flex flex-col gap-2 text-[15px]">
          {#each METHODS as [key, label] (key)}
            <li>
              <button type="button" class="flex w-full items-center gap-3" aria-pressed={method === key} onclick={() => (method = key)}>
                <span class="flex h-[19px] w-[19px] shrink-0 items-center justify-center border border-paper">
                  {#if method === key}<span class="h-3 w-3 bg-accent"></span>{/if}
                </span>
                {label}
              </button>
            </li>
          {/each}
        </ul>

        <!-- Layout: declutter toggle + anchor-strength slider. -->
        <h2 class="mt-6 text-[18px]">Layout</h2>
        <button
          type="button"
          class="mt-3 flex w-full items-center gap-3 text-[15px]"
          aria-pressed={declutter}
          onclick={() => (declutter = !declutter)}
        >
          <span class="flex h-[19px] w-[19px] shrink-0 items-center justify-center border border-paper">
            {#if declutter}<span class="h-3 w-3 bg-accent"></span>{/if}
          </span>
          Spread out (no overlap)
        </button>

        <div class="mt-3 {declutter ? '' : 'pointer-events-none opacity-40'}">
          <div class="flex items-center justify-between text-[13px] text-paper/60">
            <span>airier</span>
            <span>keep shape</span>
          </div>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            bind:value={anchorStrength}
            disabled={!declutter}
            aria-label="Layout anchor strength"
            class="mt-1 w-full accent-accent"
          />
        </div>

        <!-- Search -->
        <h2 class="mt-6 text-[18px]">Search</h2>
        <input
          type="text"
          bind:value={query}
          onkeydown={(e) => e.key === "Enter" && runSearch()}
          class="mt-2 w-full border border-paper bg-ink px-2 py-2 text-[15px] outline-none focus:border-accent"
          placeholder=""
        />

        <div class="mt-3 flex items-center gap-3 text-[15px]">
          <!-- Cosine ⇄ BM25 toggle: a two-state square, accent on the active side. -->
          <button type="button" class:text-accent={searchMethod === "cosine"} onclick={() => (searchMethod = "cosine")}> Cosine </button>
          <button
            type="button"
            class="flex h-[19px] w-[35px] items-center border border-paper p-0.5"
            role="switch"
            aria-checked={searchMethod === "bm25"}
            aria-label="Toggle between cosine and BM25 search"
            onclick={() => (searchMethod = searchMethod === "cosine" ? "bm25" : "cosine")}
          >
            <span class="h-3 w-3 bg-accent transition-transform" style="transform: translateX({searchMethod === 'bm25' ? '14px' : '0'})"></span>
          </button>
          <button type="button" class:text-accent={searchMethod === "bm25"} onclick={() => (searchMethod = "bm25")}> BM25 </button>

          <button
            type="button"
            class="ml-auto bg-accent px-5 py-1 text-paper hover:opacity-90 disabled:opacity-40"
            disabled={searching}
            onclick={runSearch}
          >
            {searching ? "…" : "Go"}
          </button>
        </div>

        <!-- Top-k: how many nearest chunks to ring on the map. -->
        <div class="mt-3">
          <div class="flex items-center justify-between text-[13px] text-paper/60">
            <span>matches</span>
            <span class="text-paper">{topK}</span>
          </div>
          <input
            type="range"
            min="1"
            max="25"
            step="1"
            bind:value={topK}
            aria-label="Number of search matches to highlight"
            class="mt-1 w-full accent-accent"
          />
        </div>

        {#if searchActive}
          <p class="mt-2 text-xs text-paper/50">
            {highlight.size} match{highlight.size === 1 ? "" : "es"} highlighted ·
            <button
              class="underline"
              onclick={() => {
                query = "";
                highlight.clear();
                searchActive = false;
              }}
            >
              clear
            </button>
          </p>
        {/if}

        <!-- Reset study progress: a quiet text link pinned to the bottom of the
             sidebar — a rare, destructive action, so it stays out of the way. -->
        <button
          type="button"
          class="mt-auto self-center text-xs text-paper/50 underline hover:text-paper disabled:opacity-40"
          disabled={resetting}
          onclick={() => (confirmReset = true)}
        >
          reset study progress
        </button>
      </aside>
    </div>
  {/if}
</div>

{#if confirmReset}
  <Modal title="Reset study progress?" onClose={() => !resetting && (confirmReset = false)}>
    <p class="text-sm text-paper/70">
      This marks every chunk as <span class="text-paper">not studied</span>, clearing all actively and passively
      studied points. This cannot be undone.
    </p>
    <div class="mt-6 flex justify-end gap-3">
      <button
        type="button"
        class="border border-paper px-4 py-1.5 text-sm hover:border-accent disabled:opacity-40"
        disabled={resetting}
        onclick={() => (confirmReset = false)}
      >
        Cancel
      </button>
      <button
        type="button"
        class="bg-accent px-4 py-1.5 text-sm text-paper hover:opacity-90 disabled:opacity-40"
        disabled={resetting}
        onclick={resetStudy}
      >
        {resetting ? "Resetting…" : "Reset"}
      </button>
    </div>
  </Modal>
{/if}
