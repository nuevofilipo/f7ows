<script>
  import { ChatStore, buildTree } from "$lib/stores/chat.svelte.js";
  import { sources } from "$lib/stores/sources.svelte.js";
  import { getSettings, updateSettings } from "$lib/api/settings.js";
  import MessageThread from "$lib/components/chat/MessageThread.svelte";
  import Composer from "$lib/components/chat/Composer.svelte";
  import ModelSelect from "./ModelSelect.svelte";
  import Spinner from "$lib/components/ui/Spinner.svelte";

  /**
   * Center column — the "Main Chat". Owns a ChatStore for the active chat.
   * The header holds the chat selector + "new chat" (left) and Chat Settings
   * (right); no bottom border so it reads as one window. Its left/right borders
   * are the shared seams with the Sources / Outputs panels.
   * @type {{
   *   projectId: string,
   *   chatId: string,
   *   chats: import('$lib/api/chats.js').ChatSummary[],
   *   onSelectChat: (id: string) => void,
   *   onNewChat: () => void,
   *   onOpenSettings?: () => void
   * }}
   */
  let { projectId, chatId, chats, onSelectChat, onNewChat, onOpenSettings } = $props();

  // New store whenever the chat switches; load its messages.
  let store = $state(new ChatStore(projectId, chatId));
  $effect(() => {
    const s = new ChatStore(projectId, chatId);
    store = s;
    s.load();
  });

  const tree = $derived(buildTree(store.messages));

  // Quick model switch (header). Seed from the user's effective settings; a change
  // persists immediately so it applies to the next turn — even mid-session.
  let chatModel = $state("");
  $effect(() => {
    getSettings()
      .then((s) => (chatModel = s.effective.chat_model))
      .catch(() => {});
  });
  /** @param {string} id */
  async function changeModel(id) {
    chatModel = id;
    try {
      await updateSettings({ chat_model: id });
    } catch {
      /* non-fatal; the model just won't persist */
    }
  }

  /** @type {HTMLElement | undefined} */
  let scroller = $state();
  // Keep the latest turn (and the typing dots) in view as messages change.
  $effect(() => {
    store.messages.length;
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  });

  /**
   * Pending send target: which message to attach to and whether the next turn
   * goes deeper (reply) or stays at the same level (continue). Null = a bare
   * send, which always continues the level-1 main thread.
   * @type {{ message: import('$lib/api/chats.js').Message, mode: 'reply' | 'continue' } | null}
   */
  let actionTarget = $state(null);

  // The most recent level-1 message — a bare Send always continues here.
  const lastLevel1 = $derived.by(() => {
    /** @type {import('$lib/api/chats.js').Message | null} */
    let found = null;
    for (const m of store.messages) if ((m.metadata?.level ?? 1) === 1) found = m;
    return found;
  });

  const lastTokens = $derived.by(() => {
    for (let i = store.messages.length - 1; i >= 0; i--) {
      const t = store.messages[i].metadata?.retrieval_tokens;
      if (typeof t === "number") return t;
    }
    return null;
  });

  // Running dollar total for this chat — sum of the per-message cost the backend
  // records from OpenRouter's reported usage.cost (see app/cost.py).
  const chatCost = $derived.by(() => {
    let sum = 0;
    for (const m of store.messages) {
      const c = m.metadata?.cost;
      if (typeof c === "number") sum += c;
    }
    return sum;
  });

  /** @param {{ content: string, command?: string }} p */
  async function handleSend({ content, command }) {
    /** @type {string | undefined} */
    let parentPath;
    let level;
    if (actionTarget) {
      const targetLevel = actionTarget.message.metadata?.level ?? 1;
      parentPath = actionTarget.message.path;
      level = actionTarget.mode === "reply" ? Math.min(targetLevel + 1, 3) : targetLevel;
    } else {
      parentPath = lastLevel1?.path;
      level = 1;
    }
    try {
      await store.send({ content, command, parentPath, level, sourceIds: sources.selectedIds });
      actionTarget = null;
    } catch {
      /* error surfaced via store.error */
    }
  }
</script>

<section class="flex h-full min-h-0 flex-col border-y border-r border-paper">
  <header class="flex items-center justify-between gap-4 px-5 py-3">
    <div class="flex items-center gap-3">
      <select
        class="border border-paper bg-ink px-2 py-1 text-sm"
        value={chatId}
        onchange={(e) => onSelectChat(/** @type {HTMLSelectElement} */ (e.currentTarget).value)}
      >
        <!-- Number oldest→newest so each chat keeps a stable label as new ones are
             added (the list arrives most-recent-first). -->
        {#each chats as c, i (c.chat_id)}
          <option value={c.chat_id}>Chat {chats.length - i} · {c.message_count} msgs</option>
        {/each}
        {#if !chats.find((c) => c.chat_id === chatId)}
          <option value={chatId}>new chat</option>
        {/if}
      </select>
      <button class="text-sm text-paper/60 hover:text-accent" onclick={onNewChat}>+ new chat</button>
    </div>
    <div class="flex items-center gap-2">
      <!-- Quick model switch — same picker as Chat Settings, persisted on change. -->
      <div class="w-44">
        {#if chatModel}
          <ModelSelect value={chatModel} onChange={changeModel} compact align="right" />
        {/if}
      </div>
      <button class="bg-accent px-3 py-1 text-[16px] text-paper hover:opacity-90" onclick={() => onOpenSettings?.()}> Chat Settings </button>
    </div>
  </header>

  <div bind:this={scroller} class="scroll-clean min-h-0 flex-1 overflow-y-auto px-5 py-5">
    {#if store.loading}
      <Spinner label="loading…" />
    {:else}
      <MessageThread
        nodes={tree}
        onReply={(m) => (actionTarget = { message: m, mode: "reply" })}
        onContinue={(m) => (actionTarget = { message: m, mode: "continue" })}
      />
    {/if}
    {#if store.error}
      <p class="mt-3 text-sm text-red-400">{store.error}</p>
    {/if}
  </div>

  <Composer
    commands={store.commands}
    sending={store.sending}
    canSend={sources.hasSelection}
    {actionTarget}
    {lastTokens}
    {chatCost}
    onSend={handleSend}
    onCancelAction={() => (actionTarget = null)}
  />
</section>
