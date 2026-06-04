<script>
	import { fade } from 'svelte/transition';
	import { renderMarkdown } from '$lib/utils/markdown.js';
	import { isGenerating, isPending } from '$lib/stores/chat.svelte.js';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import TypingDots from './TypingDots.svelte';
	import MockExam from './MockExam.svelte';

	/**
	 * One message. User turns sit in a bordered box on the right (per Figma);
	 * assistant turns render full-width markdown/KaTeX with citations.
	 * @type {{
	 *   message: import('$lib/api/chats.js').Message,
	 *   onReply?: (m: import('$lib/api/chats.js').Message) => void,
	 *   onContinue?: (m: import('$lib/api/chats.js').Message) => void
	 * }}
	 */
	let { message, onReply, onContinue } = $props();

	const isUser = $derived(message.role === 'user');
	// Slash-command a user turn ran (/mockexam, /deepsummary …), shown as a tag.
	const command = $derived(message.metadata?.command);
	const pending = $derived(isPending(message));
	const generating = $derived(isGenerating(message));
	const level = $derived(message.metadata?.level ?? 1);
	const exam = $derived(
		Array.isArray(message.metadata?.exam) ? message.metadata.exam : null
	);
	/**
	 * Map S-number -> cited chunk, so an inline `[S#]` marker maps straight to its
	 * chunk text/path for the hover popup. (Older messages without `s` simply
	 * won't have a popup.)
	 * @type {Map<number, { path: string, text: string }>}
	 */
	const citeMap = $derived.by(() => {
		const m = new Map();
		const list = Array.isArray(message.metadata?.cited) ? message.metadata.cited : [];
		for (const c of list) if (typeof c?.s === 'number') m.set(c.s, c);
		return m;
	});

	/** @type {HTMLElement | undefined} */
	let container = $state();
	/**
	 * Fixed-positioned popup. Anchored by `top` (placed below the marker) OR
	 * `bottom` (flipped above it), clamped to the visible chat area on every side;
	 * `maxHeight` shrinks it to fit (it scrolls) so it can never leave the box.
	 * @type {{ entry: { path: string, text: string }, left: number, top: number|null, bottom: number|null, width: number, maxHeight: number } | null}
	 */
	let popup = $state(null);
	/** @type {ReturnType<typeof setTimeout> | undefined} */
	let closeTimer;

	/** Open/move the chunk-peek popup when a [S#] marker is hovered or focused. */
	function onCiteEnter(/** @type {Event} */ e) {
		const el = /** @type {HTMLElement | null} */ (
			/** @type {HTMLElement} */ (e.target)?.closest?.('.cite')
		);
		if (!el || !container?.contains(el)) return;
		const entry = citeMap.get(Number(el.dataset.cite));
		if (!entry) return;
		clearTimeout(closeTimer);

		const PAD = 8; // min gap to each edge of the chat area
		const GAP = 4; // gap between marker and popup
		const MAX_W = 340;
		const MAX_H = 220;
		const r = el.getBoundingClientRect();
		// Clamp within the scrollable chat viewport (its visible rect), not the
		// whole window — so the popup also never spills over the side panels.
		const scroller = container.closest('.scroll-clean');
		const vb = scroller
			? scroller.getBoundingClientRect()
			: { left: 0, top: 0, right: innerWidth, bottom: innerHeight, width: innerWidth };

		const width = Math.min(MAX_W, vb.width - PAD * 2);
		const left = Math.max(vb.left + PAD, Math.min(r.left, vb.right - width - PAD));

		// Place on the side with more room; shrink (scrolls) to whatever fits there.
		const spaceBelow = vb.bottom - r.bottom - GAP - PAD;
		const spaceAbove = r.top - vb.top - GAP - PAD;
		if (spaceBelow >= spaceAbove) {
			popup = { entry, left, top: r.bottom + GAP, bottom: null, width,
				maxHeight: Math.min(MAX_H, Math.max(0, spaceBelow)) };
		} else {
			popup = { entry, left, top: null, bottom: innerHeight - (r.top - GAP), width,
				maxHeight: Math.min(MAX_H, Math.max(0, spaceAbove)) };
		}
	}

	const scheduleClose = () => {
		clearTimeout(closeTimer);
		closeTimer = setTimeout(() => (popup = null), 120);
	};
	const keepOpen = () => clearTimeout(closeTimer);
</script>

{#if isUser}
	<div class="flex justify-end">
		<div class="max-w-[70%] border border-paper px-4 py-3">
			<p class="whitespace-pre-wrap text-[15px] leading-relaxed">{#if command}<span class="text-accent">/{command}</span>{' '}{/if}{message.content}</p>
		</div>
	</div>
{:else}
	<div class="group max-w-none">
		{#if pending}
			<TypingDots />
		{:else if generating}
			<Spinner label="generating…" />
		{:else}
			<div bind:this={container} class="relative">
				<!-- svelte-ignore a11y_mouse_events_have_key_events -->
				<!-- Citation hover is delegated from this wrapper, so [S#] markers work
				     in both the plain answer and the MockExam (both emit .cite spans). -->
				<div
					role="presentation"
					onmouseover={onCiteEnter}
					onmouseout={scheduleClose}
					onfocusin={onCiteEnter}
					onfocusout={scheduleClose}
				>
					{#if exam}
						<MockExam {exam} />
					{:else}
						<div class="prose-chat max-w-none text-[15px] leading-relaxed">
							{@html renderMarkdown(message.content)}
						</div>
					{/if}
				</div>
				{#if popup}
					<div
						class="scroll-clean fixed z-20 overflow-y-auto border border-paper bg-ink p-3 shadow-lg"
						style="left:{popup.left}px;{popup.top != null ? ` top:${popup.top}px;` : ''}{popup.bottom != null ? ` bottom:${popup.bottom}px;` : ''} width:{popup.width}px; max-height:{popup.maxHeight}px"
						transition:fade={{ duration: 120 }}
						role="tooltip"
						onmouseenter={keepOpen}
						onmouseleave={scheduleClose}
					>
						<div class="mb-1 break-all border-b border-paper pb-1 text-xs text-paper/50">
							{popup.entry.path}
						</div>
						<div class="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-paper/80">
							{popup.entry.text}
						</div>
					</div>
				{/if}
			</div>
		{/if}
		{#if !generating && !pending && (onContinue || onReply)}
			<div
				class="mt-1 flex gap-4 text-xs text-paper/50 opacity-0 transition-opacity group-hover:opacity-100"
			>
				{#if onContinue}
					<button class="hover:text-accent" onclick={() => onContinue?.(message)}>
						⤵ continue thread
					</button>
				{/if}
				{#if onReply && level < 3}
					<button class="hover:text-accent" onclick={() => onReply?.(message)}>↳ reply</button>
				{/if}
			</div>
		{/if}
	</div>
{/if}
