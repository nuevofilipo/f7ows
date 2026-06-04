<script>
	/**
	 * Prompt box: textarea + slash-command menu + send. Matches the Figma input
	 * field (placeholder, "no chat context" toggle, token/cost readout).
	 *
	 * @type {{
	 *   commands: { name: string, description: string }[],
	 *   sending?: boolean,
	 *   canSend?: boolean,
	 *   actionTarget?: { message: import('$lib/api/chats.js').Message, mode: 'reply' | 'continue' } | null,
	 *   lastTokens?: number | null,
	 *   chatCost?: number,
	 *   onSend: (p: { content: string, command?: string }) => void,
	 *   onCancelAction?: () => void
	 * }}
	 */
	let {
		commands,
		sending = false,
		canSend = true,
		actionTarget = null,
		lastTokens = null,
		chatCost = 0,
		onSend,
		onCancelAction
	} = $props();

	// Format the running chat cost: more precision below a cent so sub-cent chats
	// still read as a real number rather than "$0.00".
	const costLabel = $derived(
		!chatCost ? '$0.00' : chatCost < 0.01 ? `$${chatCost.toFixed(4)}` : `$${chatCost.toFixed(3)}`
	);

	let text = $state('');
	/** @type {string | null} */
	let activeCommand = $state(null);

	/** @type {HTMLTextAreaElement | undefined} */
	let textarea = $state();
	// Grow with content up to ~6 lines, then scroll within the box.
	const MAX_HEIGHT = 160;
	// The natural rows=2 height, captured on mount — the floor we never shrink past.
	let minHeight = 0;

	/** Resize the textarea to fit its content, clamped to [minHeight, MAX_HEIGHT]. */
	function autoresize() {
		if (!textarea) return;
		textarea.style.height = 'auto';
		const fit = Math.min(Math.max(textarea.scrollHeight, minHeight), MAX_HEIGHT);
		textarea.style.height = `${fit}px`;
	}

	// Capture the rendered 2-row height as the floor, then fit on every change
	// (typing, and the reset back to empty after submit).
	$effect(() => {
		if (textarea && !minHeight) minHeight = textarea.clientHeight;
	});
	$effect(() => {
		text;
		autoresize();
	});

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
		if (!content || sending || !canSend) return;
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

	{#if actionTarget}
		<div class="flex items-center justify-between border-b border-paper px-3 py-1 text-xs text-paper/70">
			<span>
				{actionTarget.mode === 'reply' ? '↳ replying — one level deeper' : '⤵ continuing thread'}
			</span>
			<button class="hover:text-accent" onclick={() => onCancelAction?.()}>cancel</button>
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

	<!-- Cut vertically: textarea takes the full height on the left; the Send button
	     sits at the top of a narrow right column with the token count beneath it. -->
	<div class="flex items-stretch">
		<textarea
			bind:this={textarea}
			bind:value={text}
			onkeydown={onKeydown}
			oninput={autoresize}
			rows="2"
			disabled={sending}
			placeholder="type your prompt, or type / to see available functions"
			class="scroll-clean min-w-0 flex-1 resize-none overflow-y-auto bg-transparent px-4 py-3 text-[15px] text-paper placeholder:text-paper/40 focus:outline-none"
			style="max-height: {MAX_HEIGHT}px"
		></textarea>

		<div class="flex shrink-0 flex-col items-end gap-1 p-2">
			<button
				class="bg-accent px-4 py-1 text-paper disabled:opacity-40"
				disabled={sending || !text.trim() || !canSend}
				onclick={submit}
			>
				{sending ? '…' : 'Send'}
			</button>
			<span class="text-xs text-paper/60">
				{#if lastTokens != null}{lastTokens.toLocaleString()} ctx · {/if}{costLabel}
			</span>
		</div>
	</div>

	{#if !canSend}
		<div class="px-4 pb-1 text-xs text-paper/50">select at least one source to send</div>
	{/if}
</div>
