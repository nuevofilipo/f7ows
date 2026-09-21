<script>
  import { goto } from "$app/navigation";
  import { session } from "$lib/stores/session.svelte.js";
  import { listProjects, createProject } from "$lib/api/projects.js";
  import SourcesPanel from "./SourcesPanel.svelte";
  import ChatPanel from "./ChatPanel.svelte";
  import OutputsPanel from "./OutputsPanel.svelte";
  import ChatSettings from "./ChatSettings.svelte";

  /**
   * Notebook page layout — top bar + 3 panes (Sources | Main Chat | Outputs).
   * The panes sit flush so each adjacent edge is a single shared 1px border, and
   * the two seams are draggable (within sane min/max) to resize the side panels.
   * @type {{
   *   projectName: string,
   *   projectId: string,
   *   chatId: string,
   *   chats: import('$lib/api/chats.js').ChatSummary[],
   *   onSelectChat: (id: string) => void,
   *   onNewChat: () => void
   * }}
   */
  let { projectName, projectId, chatId, chats, onSelectChat, onNewChat } = $props();

  let settingsOpen = $state(false);
  let profileOpen = $state(false);
  let projectsOpen = $state(false);

  /** All of the user's notebooks, for the title dropdown. @type {import('$lib/api/projects.js').Project[]} */
  let projects = $state([]);
  // The other notebooks (everything but the one we're looking at).
  const otherProjects = $derived(projects.filter((p) => p.id !== projectId));

  $effect(() => {
    if (!session.authenticated) return;
    listProjects()
      .then((list) => (projects = list))
      .catch(() => {});
  });

  /** @param {string} id */
  function switchProject(id) {
    projectsOpen = false;
    if (id !== projectId) goto(`/projects/${id}`);
  }

  // Inline "new notebook" creator next to the title — a subtle "+" reveals an
  // input + Create button, so a notebook can be made without the list page.
  let creatingOpen = $state(false);
  let newName = $state("");
  let creating = $state(false);
  /** @type {HTMLInputElement | undefined} */
  let newNameInput = $state();

  function openCreate() {
    creatingOpen = true;
    // Focus the field once it's in the DOM.
    queueMicrotask(() => newNameInput?.focus());
  }

  function cancelCreate() {
    creatingOpen = false;
    newName = "";
  }

  async function create() {
    const name = newName.trim();
    if (!name || creating) return;
    creating = true;
    try {
      const project = await createProject(name);
      goto(`/projects/${project.id}`);
    } catch {
      creating = false; // surfaced by staying open; keep it simple for now
    }
  }

  // The two header dropdowns. A window click outside either one closes it. The
  // toggle buttons live inside these wrappers, so a click on a button bubbles up
  // already "inside" and never triggers a close-then-reopen.
  /** @type {HTMLElement | undefined} */
  let projectsMenu = $state();
  /** @type {HTMLElement | undefined} */
  let profileMenu = $state();

  /** @param {MouseEvent} e */
  function onWindowClick(e) {
    const t = /** @type {Node} */ (e.target);
    if (projectsOpen && projectsMenu && !projectsMenu.contains(t)) projectsOpen = false;
    if (profileOpen && profileMenu && !profileMenu.contains(t)) profileOpen = false;
  }

  // Resizable side panels (px). Clamped so the chat always keeps room. The last
  // widths are remembered in localStorage so the layout survives reloads.
  const LAYOUT_KEY = "notebook:layout";
  const savedLayout = (() => {
    if (typeof localStorage === "undefined") return {};
    try {
      return JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "{}");
    } catch {
      return {};
    }
  })();
  let sourcesWidth = $state(savedLayout.sourcesWidth ?? 340);
  let outputsWidth = $state(savedLayout.outputsWidth ?? 356);
  const MIN = 220;
  const MAX = 560;

  // Persist the layout whenever a seam is dragged (effects run in the browser only).
  $effect(() => {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ sourcesWidth, outputsWidth }));
  });

  /**
   * Drag a seam to resize the adjacent side panel. Pointer capture keeps the
   * drag alive even when the cursor leaves the thin handle.
   * @param {'sources'|'outputs'} which @param {PointerEvent} e
   */
  function startDrag(which, e) {
    e.preventDefault();
    const startX = e.clientX;
    const startW = which === "sources" ? sourcesWidth : outputsWidth;
    const handle = /** @type {HTMLElement} */ (e.currentTarget);
    handle.setPointerCapture(e.pointerId);

    /** @param {PointerEvent} ev */
    const move = (ev) => {
      const delta = ev.clientX - startX;
      // Sources grows as you drag right; Outputs grows as you drag left.
      const next = which === "sources" ? startW + delta : startW - delta;
      const clamped = Math.max(MIN, Math.min(MAX, next));
      if (which === "sources") sourcesWidth = clamped;
      else outputsWidth = clamped;
    };
    const up = () => {
      handle.releasePointerCapture(e.pointerId);
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  }

  async function logout() {
    await session.logout();
    goto("/login");
  }
</script>

<svelte:window onclick={onWindowClick} />

<div class="flex h-screen flex-col bg-ink p-2 pt-0">
  <!-- Top bar -->
  <header class="flex items-center justify-between py-0">
    <div class="flex items-center gap-3">
      <div class="relative" bind:this={projectsMenu}>
        <button
          class="flex items-center gap-2 text-[20px] hover:text-accent"
          onclick={() => (projectsOpen = !projectsOpen)}
          aria-haspopup="listbox"
          aria-expanded={projectsOpen}
        >
          <span>{projectName}</span>
          <!-- Clean down-triangle: the only affordance that this is a dropdown. -->
          <svg width="10" height="6" viewBox="0 0 10 6" aria-hidden="true" class="opacity-70">
            <path d="M0 0 L5 6 L10 0 Z" fill="currentColor" />
          </svg>
        </button>
        {#if projectsOpen}
          <div class="absolute left-0 z-50 mt-2 w-56 border border-paper bg-ink p-1 text-sm" role="listbox">
            {#if otherProjects.length}
              {#each otherProjects as p (p.id)}
                <button
                  class="block w-full truncate px-3 py-1.5 text-left text-paper hover:bg-paper/10"
                  role="option"
                  aria-selected="false"
                  title={p.name}
                  onclick={() => switchProject(p.id)}
                >
                  {p.name}
                </button>
              {/each}
            {:else}
              <p class="px-3 py-1.5 text-paper/50">No other notebooks</p>
            {/if}
          </div>
        {/if}
      </div>

      <!-- Inline new-notebook creator: a subtle "+" toggles open into a "−"; the name
         field + Create appear to its right. Clicking the "−" closes it again. -->
      <button
        class="text-2xl leading-none text-paper/50 hover:text-accent"
        onclick={() => (creatingOpen ? cancelCreate() : openCreate())}
        aria-label={creatingOpen ? "Cancel new notebook" : "New notebook"}
        aria-expanded={creatingOpen}
        title={creatingOpen ? "Cancel" : "New notebook"}
      >
        {creatingOpen ? "−" : "+"}
      </button>
      {#if creatingOpen}
        <form
          class="flex items-center gap-2"
          onsubmit={(e) => {
            e.preventDefault();
            create();
          }}
        >
          <input
            bind:this={newNameInput}
            bind:value={newName}
            placeholder="Enter project name"
            class=" bg-ink px-2 py-1 text-sm focus:outline-none"
            onkeydown={(e) => e.key === "Escape" && cancelCreate()}
          />
          <button
            type="submit"
            class="bg-accent px-3 py-1 text-sm text-paper hover:opacity-90 disabled:opacity-40"
            disabled={creating || !newName.trim()}
          >
            {creating ? "Creating…" : "Create"}
          </button>
        </form>
      {/if}
    </div>

    <nav class="flex items-center gap-6 text-[20px]">
      <a href={`/projects/${projectId}/projection`} class="hover:text-accent">Projection</a>
      <div class="relative" bind:this={profileMenu}>
        <button onclick={() => (profileOpen = !profileOpen)}>My Profile</button>
        {#if profileOpen}
          <div class="absolute right-0 z-50 mt-2 w-56 border border-paper bg-ink p-3 text-sm">
            <p class="truncate text-paper/70">{session.user?.email}</p>
            <a href="/profile" class="mt-2 block w-full border border-paper px-3 py-1 text-center text-paper hover:bg-paper/10">Settings</a>
            {#if !session.singleUser}
              <button class="mt-2 w-full bg-accent px-3 py-1 text-paper hover:opacity-90" onclick={logout}>Log out</button>
            {/if}
          </div>
        {/if}
      </div>
    </nav>
  </header>

  <!-- Panes: flush so each shared edge is a single 1px border; seams drag to resize. -->
  <div class="flex min-h-0 flex-1">
    <div class="shrink-0" style="width: {sourcesWidth}px">
      <SourcesPanel {projectId} />
    </div>

    <div
      class="relative z-10 -mx-0.5 w-1 shrink-0 cursor-col-resize select-none touch-none hover:bg-accent"
      onpointerdown={(e) => startDrag("sources", e)}
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize sources panel"
    ></div>

    <div class="flex min-h-0 min-w-0 flex-1 flex-col">
      {#key chatId}
        <ChatPanel {projectId} {chatId} {chats} {onSelectChat} {onNewChat} onOpenSettings={() => (settingsOpen = true)} />
      {/key}
    </div>

    <div
      class="relative z-10 -mx-0.5 w-1 shrink-0 cursor-col-resize select-none touch-none hover:bg-accent"
      onpointerdown={(e) => startDrag("outputs", e)}
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize outputs panel"
    ></div>

    <div class="shrink-0" style="width: {outputsWidth}px">
      <OutputsPanel />
    </div>
  </div>
</div>

{#if settingsOpen}
  <ChatSettings onClose={() => (settingsOpen = false)} />
{/if}
