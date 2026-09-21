<script>
  import {
    getSettings,
    updateSettings,
    setOpenRouterKey,
    clearOpenRouterKey,
    getProviders,
    updateProvider,
  } from "$lib/api/settings.js";
  import { listProjects, deleteProject, exportProjectUrl } from "$lib/api/projects.js";
  import { session } from "$lib/stores/session.svelte.js";
  import { ACCENT_PRESETS, DEFAULT_ACCENT, applyAccent, setAccent } from "$lib/utils/theme.js";
  import Button from "$lib/components/ui/Button.svelte";
  import Modal from "$lib/components/ui/Modal.svelte";
  import Spinner from "$lib/components/ui/Spinner.svelte";

  /** @type {import('$lib/api/settings.js').SettingsPayload | null} */
  let payload = $state(null);
  /** @type {Record<string, any>} */
  let values = $state({});
  let loading = $state(true);
  let saving = $state(false);
  /** @type {string|null} */
  let error = $state(null);
  let savedFlash = $state(false);

  // OpenRouter key — write-only: we show whether one is set + a masked preview,
  // and let the user replace or clear it. The plaintext is never read back.
  let keySet = $state(false);
  /** @type {string|null} */
  let keyPreview = $state(null);
  let keyInput = $state("");
  let keySaving = $state(false);
  /** @type {string|null} */
  let keyError = $state(null);

  // Model providers — one endpoint/model/key_ref per role. Edited as a local copy
  // so a half-typed base URL is never sent; each row saves on its own.
  /** @type {import('$lib/api/settings.js').Provider[]} */
  let providers = $state([]);
  /** @type {string|null} */
  let providerSaving = $state(null);
  /** @type {string|null} */
  let providerError = $state(null);
  /** @type {string|null} */
  let providerSavedFlash = $state(null);

  /** @type {Record<string, string>} */
  const ROLE_HELP = {
    chat: "Writes the answers. The expensive one.",
    planner: "Cheap classifier behind retrieval — many small calls.",
    embed: "Vectors for retrieval. Changing the model needs a re-embed.",
  };

  const accent = $derived(values.accent_color ?? DEFAULT_ACCENT);

  $effect(() => {
    (async () => {
      try {
        const p = await getSettings();
        payload = p;
        values = { ...p.effective };
        keySet = p.openrouter_key_set;
        keyPreview = p.openrouter_key_preview;
        providers = (await getProviders()).providers;
        // Sync the saved accent to localStorage + the document on load.
        setAccent(values.accent_color ?? DEFAULT_ACCENT);
      } catch (/** @type {any} */ err) {
        error = err?.message ?? "Failed to load profile";
      } finally {
        loading = false;
      }
    })();
  });

  /** Save one provider row. @param {import('$lib/api/settings.js').Provider} p */
  async function saveProvider(p) {
    providerSaving = p.role;
    providerError = null;
    try {
      const res = await updateProvider(p.role, {
        base_url: p.base_url.trim(),
        model: p.model.trim(),
        key_ref: p.key_ref.trim(),
      });
      providers = res.providers;
      providerSavedFlash = p.role;
      setTimeout(() => (providerSavedFlash = null), 1500);
    } catch (/** @type {any} */ err) {
      providerError = err?.message ?? "Failed to save provider";
    } finally {
      providerSaving = null;
    }
  }

  /** Live-preview an accent without persisting (commit happens on Save). @param {string} hex */
  function pickAccent(hex) {
    values.accent_color = hex;
    applyAccent(hex);
  }

  async function save() {
    if (!payload) return;
    saving = true;
    error = null;
    // Send only changed chat + appearance fields (a small patch); projection
    // knobs are managed on the map page, not here.
    /** @type {Record<string, any>} */
    const patch = {};
    for (const f of payload.schema) {
      if (f.group === "projection") continue;
      if (values[f.key] !== payload.effective[f.key]) patch[f.key] = values[f.key];
    }
    try {
      const updated = await updateSettings(patch);
      payload = updated;
      values = { ...updated.effective };
      setAccent(values.accent_color ?? DEFAULT_ACCENT);
      savedFlash = true;
      setTimeout(() => (savedFlash = false), 1500);
    } catch (/** @type {any} */ err) {
      error = err?.message ?? "Failed to save settings";
    } finally {
      saving = false;
    }
  }

  async function saveKey() {
    const key = keyInput.trim();
    if (!key || keySaving) return;
    keySaving = true;
    keyError = null;
    try {
      const status = await setOpenRouterKey(key);
      keySet = status.openrouter_key_set;
      keyPreview = status.openrouter_key_preview;
      keyInput = "";
    } catch (/** @type {any} */ err) {
      keyError = err?.detail ?? err?.message ?? "Failed to save key";
    } finally {
      keySaving = false;
    }
  }

  async function clearKey() {
    if (keySaving) return;
    keySaving = true;
    keyError = null;
    try {
      const status = await clearOpenRouterKey();
      keySet = status.openrouter_key_set;
      keyPreview = status.openrouter_key_preview;
    } catch (/** @type {any} */ err) {
      keyError = err?.detail ?? err?.message ?? "Failed to clear key";
    } finally {
      keySaving = false;
    }
  }

  // Projects — list all notebooks and let the user delete one (with a confirm
  // modal). Deletion cascades server-side to its sources, chats and outputs.
  /** @type {import('$lib/api/projects.js').Project[]} */
  let projects = $state([]);
  /** The project pending a delete confirmation, or null. @type {import('$lib/api/projects.js').Project | null} */
  let confirmDelete = $state(null);
  let deleting = $state(false);
  /** @type {string|null} */
  let projectsError = $state(null);

  $effect(() => {
    listProjects()
      .then((list) => (projects = list))
      .catch((/** @type {any} */ err) => (projectsError = err?.message ?? "Failed to load projects"));
  });

  async function doDelete() {
    if (!confirmDelete || deleting) return;
    deleting = true;
    projectsError = null;
    try {
      await deleteProject(confirmDelete.id);
      projects = projects.filter((p) => p.id !== confirmDelete?.id);
      confirmDelete = null;
    } catch (/** @type {any} */ err) {
      projectsError = err?.message ?? "Failed to delete project";
    } finally {
      deleting = false;
    }
  }
</script>

<!-- Brutalist two-column settings: tiny top-left label, hairline-bordered boxes,
     sharp corners, sits within one screen (no page scroll). -->
<div class="flex h-screen flex-col overflow-hidden px-6 py-5">
  <header class="mb-6 flex items-baseline gap-4">
    <h1 class="text-2xl">Settings</h1>
    {#if session.user?.email}
      <span class="text-xs text-paper/40">{session.user.email}</span>
    {/if}
    <button
      type="button"
      onclick={() => history.back()}
      class="ml-auto text-sm text-paper/50 hover:text-paper"
    >← Return</button>
  </header>

  {#if loading}
    <Spinner label="loading…" />
  {:else if error && !payload}
    <p class="text-sm text-red-400">{error}</p>
  {:else}
    <div class="grid min-h-0 flex-1 grid-cols-1 gap-x-12 gap-y-8 md:grid-cols-2">
      <!-- LEFT COLUMN: OpenRouter key, then colour theme -->
      <div class="flex min-h-0 flex-col gap-8">
        <!-- OpenRouter key (BYOK) — stored encrypted; only a masked preview comes back. -->
        <section>
          <h2 class="mb-3 text-base text-paper">OpenRouter key</h2>

          <p class="mb-3 text-sm">
            {#if keySet}
              <span class="text-paper">set</span>
              <span class="text-paper">{keyPreview}</span>
            {:else}
              <span class="text-paper">Please set your OpenRouter key</span>
            {/if}
          </p>

          <form
            class="flex gap-2"
            onsubmit={(e) => {
              e.preventDefault();
              saveKey();
            }}
          >
            <input
              type="password"
              bind:value={keyInput}
              placeholder={keySet ? "Replace key (sk-or-…)" : "sk-or-…"}
              autocomplete="off"
              spellcheck="false"
              class="flex-1 border border-paper bg-ink px-3 py-2 text-sm focus:outline-none"
            />
            <Button type="submit" disabled={keySaving || !keyInput.trim()}>
              {keySaving ? "…" : keySet ? "Replace" : "Save"}
            </Button>
            {#if keySet}
              <Button variant="outline" onclick={clearKey} disabled={keySaving}>Clear</Button>
            {/if}
          </form>
          {#if keyError}<p class="mt-2 text-sm text-red-400">{keyError}</p>{/if}
        </section>

        <!-- Model providers — any OpenAI-compatible endpoint, per role. -->
        <section>
          <h2 class="mb-1 text-base text-paper">Model providers</h2>
          <p class="mb-3 text-sm text-paper/60">
            Any OpenAI-compatible endpoint. For a local Ollama use
            <code class="text-paper/80">http://localhost:11434/v1</code> and leave the key ref empty —
            local models need no key, and cost shows as “no cost” instead of $0.00.
          </p>

          <div class="flex flex-col gap-4">
            {#each providers as p (p.role)}
              <div class="border border-paper/40 p-3">
                <div class="mb-2 flex items-baseline justify-between">
                  <span class="text-sm text-paper capitalize">{p.role}</span>
                  <span class="text-xs text-paper/50">{ROLE_HELP[p.role] ?? ""}</span>
                </div>
                <div class="flex flex-col gap-2">
                  <input
                    bind:value={p.base_url}
                    placeholder="https://openrouter.ai/api/v1"
                    spellcheck="false"
                    class="border border-paper bg-ink px-3 py-1.5 text-sm focus:outline-none"
                  />
                  <input
                    bind:value={p.model}
                    placeholder="model id"
                    spellcheck="false"
                    class="border border-paper bg-ink px-3 py-1.5 text-sm focus:outline-none"
                  />
                  <div class="flex items-center gap-2">
                    <input
                      bind:value={p.key_ref}
                      placeholder="key ref (empty = no auth)"
                      spellcheck="false"
                      class="flex-1 border border-paper bg-ink px-3 py-1.5 text-sm focus:outline-none"
                    />
                    <Button onclick={() => saveProvider(p)} disabled={providerSaving === p.role}>
                      {providerSaving === p.role ? "…" : providerSavedFlash === p.role ? "Saved" : "Save"}
                    </Button>
                  </div>
                  {#if p.key_ref && !p.key_available}
                    <p class="text-xs text-red-400">
                      No key stored under “{p.key_ref}” — this role will fail until one is set.
                    </p>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
          {#if providerError}<p class="mt-2 text-sm text-red-400">{providerError}</p>{/if}
        </section>

        <!-- Colour theme -->
        <section>
          <h2 class="mb-3 text-base text-paper">Colour theme</h2>
          <div class="flex items-center gap-4">
            <div class="flex gap-2">
              {#each ACCENT_PRESETS as preset (preset)}
                <button
                  type="button"
                  onclick={() => pickAccent(preset)}
                  aria-label={`Use ${preset}`}
                  aria-pressed={accent.toLowerCase() === preset.toLowerCase()}
                  class="h-7 w-7 border transition-transform hover:scale-110 {accent.toLowerCase() === preset.toLowerCase()
                    ? 'border-paper'
                    : 'border-transparent'}"
                  style="background-color: {preset}"
                ></button>
              {/each}
            </div>

            <span class="text-paper/30">|</span>

            <!-- Custom picker: native swatch + editable hex, kept in sync. -->
            <label class="flex items-center gap-2">
              <span class="sr-only">Custom colour</span>
              <input
                type="color"
                value={accent}
                oninput={(e) => pickAccent(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
                class="h-7 w-9 cursor-pointer border border-paper/40 bg-ink p-0.5"
              />
              <input
                type="text"
                value={accent}
                oninput={(e) => pickAccent(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
                spellcheck="false"
                class="w-24 border border-paper/40 bg-ink px-2 py-1 text-sm focus:border-accent focus:outline-none"
              />
            </label>
          </div>
        </section>

        <!-- Projects — list every notebook with a delete (confirmed via modal). -->
        <section class="flex min-h-0 flex-col">
          <h2 class="mb-3 text-base text-paper">Projects</h2>
          {#if projectsError}<p class="mb-2 text-sm text-red-400">{projectsError}</p>{/if}
          {#if projects.length === 0}
            <p class="text-sm text-paper/40">No projects yet.</p>
          {:else}
            <ul class="scroll-clean flex min-h-0 flex-col divide-y divide-paper/20 overflow-y-auto border border-paper">
              {#each projects as p (p.id)}
                <li class="flex items-center justify-between gap-3 px-3 py-2 text-sm">
                  <span class="min-w-0 flex-1 truncate" title={p.name}>{p.name}</span>
                  <!-- A plain link, so the browser downloads it with the filename
                       the server chose instead of buffering a blob in memory. -->
                  <a
                    href={exportProjectUrl(p.id)}
                    class="shrink-0 text-paper/50 hover:text-accent"
                    title="Download this project (sources + chats) as a zip"
                  >Export</a>
                  <button
                    type="button"
                    class="shrink-0 text-paper/50 hover:text-red-400"
                    onclick={() => (confirmDelete = p)}
                    aria-label={`Delete ${p.name}`}
                  >Delete</button>
                </li>
              {/each}
            </ul>
          {/if}
        </section>
      </div>

      <!-- RIGHT COLUMN: system prompt, filling the height -->
      <div class="flex min-h-0 flex-col">
        <h2 class="mb-3 text-base text-paper">System prompt</h2>
        <textarea
          bind:value={values.system_prompt}
          placeholder="Steer every chat — tone, format, what to assume…"
          class="scroll-clean min-h-0 flex-1 resize-none border border-paper bg-ink px-3 py-2 text-sm leading-relaxed focus:outline-none"
        ></textarea>

        <!-- Save commits the system prompt + colour theme together. -->
        <div class="mt-4 flex items-center justify-end gap-3">
          {#if error && payload}<span class="text-sm text-red-400">{error}</span>{/if}
          {#if savedFlash}<span class="text-sm text-paper/50">Saved.</span>{/if}
          <Button onclick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        </div>
      </div>
    </div>
  {/if}
</div>

{#if confirmDelete}
  <Modal title="Delete project" onClose={() => (confirmDelete = null)}>
    <p class="text-sm text-paper">
      Delete <span class="text-accent">{confirmDelete.name}</span>? This permanently
      removes its sources, chats and outputs. This cannot be undone.
    </p>
    {#if projectsError}<p class="mt-2 text-sm text-red-400">{projectsError}</p>{/if}
    <div class="mt-5 flex justify-end gap-3">
      <Button variant="outline" onclick={() => (confirmDelete = null)} disabled={deleting}>Cancel</Button>
      <Button onclick={doDelete} disabled={deleting}>{deleting ? "Deleting…" : "Delete"}</Button>
    </div>
  </Modal>
{/if}
