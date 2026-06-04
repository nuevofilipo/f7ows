<script>
  import { getModels } from "$lib/api/settings.js";
  import Spinner from "$lib/components/ui/Spinner.svelte";

  /**
   * Searchable model picker. Lazily fetches the gateway's model catalogue the
   * first time it's opened (never on mount) — until then it just shows `value`.
   * Used both in Chat Settings and as the compact quick-switch in the chat header.
   * @type {{
   *   value: string,
   *   onChange: (id: string) => void,
   *   compact?: boolean,
   *   align?: 'left' | 'right'
   * }}
   */
  let { value, onChange, compact = false, align = "left" } = $props();

  let open = $state(false);
  let query = $state("");
  /** @type {import('$lib/api/settings.js').ModelOption[]} */
  let models = $state([]);
  let loading = $state(false);
  /** @type {string | null} */
  let error = $state(null);
  let loaded = false;

  /** @type {HTMLElement | undefined} */
  let root = $state();
  /** @type {HTMLInputElement | undefined} */
  let search = $state();

  // Fetch once, on first open. No cache invalidation by design.
  async function ensureLoaded() {
    if (loaded || loading) return;
    loading = true;
    error = null;
    try {
      models = (await getModels()).models;
      loaded = true;
    } catch (/** @type {any} */ err) {
      error = err?.message ?? "Failed to load models";
    } finally {
      loading = false;
    }
  }

  function toggle() {
    open = !open;
    if (open) ensureLoaded();
  }

  // Focus the search box as soon as the menu opens.
  $effect(() => {
    if (open && search) search.focus();
  });

  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return models;
    return models.filter(
      (m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q),
    );
  });

  // The closed button always shows the model id with the "provider/" prefix
  // stripped (never the fetched display name), so the label is stable before and
  // after the list loads. The search list below keeps the full name + id.
  const label = $derived(
    value ? (value.includes("/") ? value.slice(value.indexOf("/") + 1) : value) : "select model",
  );

  /** @param {string} id */
  function pick(id) {
    onChange(id);
    open = false;
    query = "";
  }

  /** @param {MouseEvent} e */
  function onWindowClick(e) {
    if (open && root && !root.contains(/** @type {Node} */ (e.target))) open = false;
  }
</script>

<svelte:window onclick={onWindowClick} />

<div class="relative" bind:this={root}>
  <button
    type="button"
    class="flex w-full items-center justify-between gap-2 border border-paper bg-ink px-2 py-1 text-left {compact ? 'text-sm' : ''}"
    onclick={toggle}
    aria-haspopup="listbox"
    aria-expanded={open}
  >
    <span class="truncate" title={value}>{label}</span>
    <svg width="10" height="6" viewBox="0 0 10 6" aria-hidden="true" class="shrink-0 opacity-70">
      <path d="M0 0 L5 6 L10 0 Z" fill="currentColor" />
    </svg>
  </button>

  {#if open}
    <div
      class="absolute z-50 mt-1 w-full min-w-[280px] overflow-hidden border border-paper bg-ink {align === 'right' ? 'right-0' : 'left-0'}"
      role="listbox"
    >
      <input
        bind:this={search}
        bind:value={query}
        class="w-full border-b border-paper bg-ink px-2 py-1.5 text-sm focus:outline-none"
        placeholder="search models…"
      />
      <div class="scroll-clean max-h-64 overflow-y-auto">
        {#if loading}
          <div class="px-2 py-3"><Spinner size={12} label="loading models…" /></div>
        {:else if error}
          <p class="px-2 py-3 text-xs text-red-400">{error}</p>
        {:else if filtered.length === 0}
          <p class="px-2 py-3 text-xs text-paper/50">no models match</p>
        {:else}
          {#each filtered as m (m.id)}
            <button
              type="button"
              role="option"
              aria-selected={m.id === value}
              class="block w-full px-2 py-1.5 text-left text-sm hover:bg-paper/10 {m.id === value ? 'text-accent' : ''}"
              onclick={() => pick(m.id)}
            >
              <span class="block truncate">{m.name}</span>
              <span class="block truncate text-xs text-paper/40">{m.id}</span>
            </button>
          {/each}
        {/if}
      </div>
    </div>
  {/if}
</div>
