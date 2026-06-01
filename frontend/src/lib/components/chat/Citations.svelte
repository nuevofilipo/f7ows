<script>
	/**
	 * Citation chips for an assistant message — the chunks the answer actually
	 * referenced via [S#] markers (message.metadata.cited).
	 * @type {{ cited?: { id: string, path: string }[] }}
	 */
	let { cited = [] } = $props();
</script>

{#if cited.length}
	<div class="mt-2 flex flex-wrap gap-1.5">
		{#each cited as c, i}
			<span
				class="border border-paper px-2 py-0.5 text-xs text-paper/80"
				title={c.path}
			>
				[S{i + 1}] {c.path}
			</span>
		{/each}
	</div>
{/if}
