<script>
  import { updateProject } from "$lib/api/projects.js";
  import { setOpenRouterKey } from "$lib/api/settings.js";
  import Modal from "$lib/components/ui/Modal.svelte";
  import Button from "$lib/components/ui/Button.svelte";

  /**
   * First-run onboarding gate. The project already exists (created in the
   * background) with a placeholder name; here we require the user to name it and
   * provide an OpenRouter key before they can use it. Forced modal — no close
   * until both are saved.
   * @type {{
   *   projectId: string,
   *   initialName?: string,
   *   onDone: (name: string) => void
   * }}
   */
  let { projectId, initialName = "", onDone } = $props();

  // Don't seed the field with the placeholder name — make the user type a real one.
  let name = $state("");
  let key = $state("");
  let saving = $state(false);
  /** @type {string|null} */
  let error = $state(null);

  const canSubmit = $derived(!!name.trim() && !!key.trim() && !saving);

  async function submit() {
    if (!canSubmit) return;
    saving = true;
    error = null;
    try {
      // Save the key first: if it's rejected, the name rename hasn't happened yet
      // and the user just fixes the key and retries.
      await setOpenRouterKey(key.trim());
      const project = await updateProject(projectId, { name: name.trim() });
      onDone(project.name);
    } catch (/** @type {any} */ err) {
      error = err?.detail ?? err?.message ?? "Couldn't save. Check your key and try again.";
      saving = false;
    }
  }
</script>

<Modal title="Set up your notebook" dismissable={false} onClose={() => {}}>
  <p class="mb-4 text-sm text-paper/60">
    Name your first notebook and add your OpenRouter key to get started.
  </p>

  <form
    class="flex flex-col gap-4"
    onsubmit={(e) => {
      e.preventDefault();
      submit();
    }}
  >
    <label class="flex flex-col gap-1">
      <span class="text-sm text-paper">Notebook name</span>
      <input
        bind:value={name}
        placeholder="e.g. Organic Chemistry"
        class="border border-paper bg-ink px-3 py-2 text-sm focus:outline-none"
      />
    </label>

    <label class="flex flex-col gap-1">
      <span class="text-sm text-paper">OpenRouter key</span>
      <input
        type="password"
        bind:value={key}
        placeholder="sk-or-…"
        autocomplete="off"
        spellcheck="false"
        class="border border-paper bg-ink px-3 py-2 text-sm focus:outline-none"
      />
      <span class="text-xs text-paper/40">Stored encrypted; used for all your model traffic.</span>
    </label>

    {#if error}<p class="text-sm text-red-400">{error}</p>{/if}

    <div class="flex justify-end">
      <Button type="submit" disabled={!canSubmit}>{saving ? "Saving…" : "Continue"}</Button>
    </div>
  </form>
</Modal>
