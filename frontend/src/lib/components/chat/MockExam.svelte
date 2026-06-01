<script>
	import { renderMarkdown } from '$lib/utils/markdown.js';

	/**
	 * /mockexam output — question/answer pairs (message.metadata.exam). Each
	 * answer is hidden behind a toggle so you can self-test before revealing.
	 * @type {{ exam: { question: string, answer: string }[] }}
	 */
	let { exam } = $props();

	let open = $state(/** @type {Record<number, boolean>} */ ({}));
</script>

<div class="flex flex-col gap-4">
	{#each exam as item, i}
		<div class="border border-paper p-3">
			<div class="prose-chat max-w-none text-paper">
				<span class="mr-1 font-bold">Q{i + 1}.</span>
				{@html renderMarkdown(item.question)}
			</div>
			<button
				class="mt-2 text-sm text-accent underline"
				onclick={() => (open[i] = !open[i])}
			>
				{open[i] ? 'Hide answer' : 'Show answer'}
			</button>
			{#if open[i]}
				<div class="prose-chat mt-2 max-w-none border-t border-paper pt-2">
					{@html renderMarkdown(item.answer)}
				</div>
			{/if}
		</div>
	{/each}
</div>
