<script>
	/**
	 * Prompt box: textarea + slash-command menu + send. Matches the Figma input
	 * field (placeholder, "no chat context" toggle, token/cost readout).
	 *
	 * @type {{
	 *   commands: { name: string, description: string }[],
	 *   sending?: boolean,
	 *   replyingTo?: import('$lib/api/chats.js').Message | null,
	 *   lastTokens?: number | null,
	 *   onSend: (p: { content: string, command?: string }) => void,
	 *   onCancelReply?: () => void
	 * }}
	 */
	let {
		commands,
		sending = false,
		replyingTo = null,
		lastTokens = null,
		onSend,
		onCancelReply
	} = $props();

	let text = $state('');
	/** @type {string | null} */
	let activeCommand = $state(null);

	// Show the command menu while the user is typing a leading "/token".
	const slashQuery = $derived(/^\/(\w*)$/.exec(text.trimStart())?.[1] ?? null);
	const menuOpen = $derived(slashQuery !== null && !activeCommand);
	const filtered = $derived(
		menuOpen ? commands.filter((c) => c.name.startsWith(slashQuery ?? '')) : []
	);

	/** @param {{ name: string }} cmd */
	function pickCommand(cmd) {
		activeCommand = cmd.name;
		text = '';
	}

	function submit() {
		const content = text.trim();
		if (!content || sending) return;
		onSend({ content, command: activeCommand ?? undefined });
		text = '';
		activeCommand = null;
	}

	/** @param {KeyboardEvent} e */
	function onKeydown(e) {
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submit();
		}
	}
</script>

<div class="relative border-t border-paper">
	{#if menuOpen && filtered.length}
		<ul class="absolute bottom-full left-0 mb-1 w-full border border-paper bg-ink">
			{#each filtered as cmd}
				<li>
					<button
						class="flex w-full flex-col gap-0.5 px-3 py-2 text-left hover:bg-paper/10"
						onclick={() => pickCommand(cmd)}
					>
						<span class="text-accent">/{cmd.name}</span>
						<span class="text-xs text-paper/60">{cmd.description}</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}

	{#if replyingTo}
		<div class="flex items-center justify-between border-b border-paper px-3 py-1 text-xs text-paper/70">
			<span>↳ replying in thread</span>
			<button class="hover:text-accent" onclick={() => onCancelReply?.()}>cancel</button>
		</div>
	{/if}

	{#if activeCommand}
		<div class="flex items-center gap-2 px-3 pt-2">
			<span class="flex items-center gap-1 bg-accent px-2 py-0.5 text-sm text-paper">
				/{activeCommand}
				<button class="font-bold" onclick={() => (activeCommand = null)}>×</button>
			</span>
		</div>
	{/if}

	<textarea
		bind:value={text}
		onkeydown={onKeydown}
		rows="2"
		disabled={sending}
		placeholder="type your prompt, or type / to see available functions"
		class="w-full resize-none bg-transparent px-4 py-3 text-[15px] text-paper placeholder:text-paper/40 focus:outline-none"
	></textarea>

	<div class="flex items-center justify-end px-4 pb-2 text-xs text-paper/60">
		<div class="flex items-center gap-3">
			{#if lastTokens != null}
				<span>{lastTokens.toLocaleString()} ctx tokens</span>
			{/if}
			<button
				class="bg-accent px-4 py-1 text-paper disabled:opacity-40"
				disabled={sending || !text.trim()}
				onclick={submit}
			>
				{sending ? '…' : 'Send'}
			</button>
		</div>
	</div>
</div>
