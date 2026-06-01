<script>
	import MessageBubble from './MessageBubble.svelte';
	import Self from './MessageThread.svelte';

	/**
	 * Renders a message tree. The backend nests every turn, so the *main line* is
	 * the chain of first-children — it stays flat with no depth bar. Any extra
	 * child of a message is a *branch* (created by replying to that message): it
	 * renders indented under that message, BEFORE the main line continues, with a
	 * clickable white bar that collapses the whole branch. Recursive to any depth.
	 * @type {{
	 *   nodes: { message: import('$lib/api/chats.js').Message, children: any[] }[],
	 *   onReply?: (m: import('$lib/api/chats.js').Message) => void,
	 *   onContinue?: (m: import('$lib/api/chats.js').Message) => void
	 * }}
	 */
	let { nodes, onReply, onContinue } = $props();

	/** Per-branch collapse state, keyed by the branch root message id. */
	let collapsed = $state(/** @type {Record<string, boolean>} */ ({}));
</script>

<div class="flex flex-col gap-5">
	{#each nodes as node (node.message.id)}
		{@const level = node.message.metadata?.level ?? 1}
		{@const branches = node.children.filter((c) => (c.message.metadata?.level ?? 1) > level)}
		{@const conts = node.children.filter((c) => (c.message.metadata?.level ?? 1) <= level)}
		<div class="flex flex-col gap-5">
			<MessageBubble message={node.message} {onReply} {onContinue} />

			<!-- Reply branches: one level deeper, indented under this message with a
			     collapsible bar, before the same-level continuation. -->
			{#each branches as branch (branch.message.id)}
				<div class="flex gap-2">
					<button
						type="button"
						class="w-3 shrink-0 cursor-pointer self-stretch border-l border-paper transition-colors hover:border-accent"
						aria-label={collapsed[branch.message.id] ? 'Expand reply' : 'Collapse reply'}
						onclick={() => (collapsed[branch.message.id] = !collapsed[branch.message.id])}
					></button>
					<div class="min-w-0 flex-1">
						{#if collapsed[branch.message.id]}
							<button
								type="button"
								class="block max-w-full truncate text-left text-sm text-paper/40 hover:text-accent"
								onclick={() => (collapsed[branch.message.id] = false)}
							>
								⋯ {branch.message.content || 'reply'}
							</button>
						{:else}
							<Self nodes={[branch]} {onReply} {onContinue} />
						{/if}
					</div>
				</div>
			{/each}

			<!-- Same-level continuation stays flat (no bar). -->
			{#if conts.length}
				<Self nodes={conts} {onReply} {onContinue} />
			{/if}
		</div>
	{/each}
</div>
