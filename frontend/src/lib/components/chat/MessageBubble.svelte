<script>
	import { renderMarkdown } from '$lib/utils/markdown.js';
	import { isGenerating, isPending } from '$lib/stores/chat.svelte.js';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import TypingDots from './TypingDots.svelte';
	import Citations from './Citations.svelte';
	import MockExam from './MockExam.svelte';

	/**
	 * One message. User turns sit in a bordered box on the right (per Figma);
	 * assistant turns render full-width markdown/KaTeX with citations.
	 * @type {{
	 *   message: import('$lib/api/chats.js').Message,
	 *   onReply?: (m: import('$lib/api/chats.js').Message) => void
	 * }}
	 */
	let { message, onReply } = $props();

	const isUser = $derived(message.role === 'user');
	const pending = $derived(isPending(message));
	const generating = $derived(isGenerating(message));
	const exam = $derived(
		Array.isArray(message.metadata?.exam) ? message.metadata.exam : null
	);
	const cited = $derived(
		Array.isArray(message.metadata?.cited) ? message.metadata.cited : []
	);
</script>

{#if isUser}
	<div class="flex justify-end">
		<div class="max-w-[70%] border border-paper px-4 py-3">
			<p class="whitespace-pre-wrap text-[15px] leading-relaxed">{message.content}</p>
		</div>
	</div>
{:else}
	<div class="group max-w-none">
		{#if pending}
			<TypingDots />
		{:else if generating}
			<Spinner label="generating…" />
		{:else if exam}
			<MockExam {exam} />
		{:else}
			<div class="prose-chat max-w-none text-[15px] leading-relaxed">
				{@html renderMarkdown(message.content)}
			</div>
		{/if}
		<Citations {cited} />
		{#if onReply && !generating && !pending}
			<button
				class="mt-1 text-xs text-paper/50 opacity-0 transition-opacity hover:text-accent group-hover:opacity-100"
				onclick={() => onReply?.(message)}
			>
				↳ reply
			</button>
		{/if}
	</div>
{/if}
