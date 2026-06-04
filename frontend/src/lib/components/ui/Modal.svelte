<script>
	/**
	 * Minimal centered modal with a backdrop. Closes on backdrop click or Esc.
	 * Pass `dismissable={false}` for a forced modal (no backdrop/Esc close, no ×) —
	 * e.g. first-run onboarding that must be completed.
	 * @type {{ title?: string, onClose: () => void, dismissable?: boolean, children?: import('svelte').Snippet }}
	 */
	let { title = '', onClose, dismissable = true, children } = $props();

	/** @param {KeyboardEvent} e */
	function onKeydown(e) {
		if (dismissable && e.key === 'Escape') onClose();
	}
</script>

<svelte:window onkeydown={onKeydown} />

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
	onclick={() => dismissable && onClose()}
	role="presentation"
>
	<div
		class="scroll-clean max-h-[85vh] w-full max-w-lg overflow-y-auto border border-paper bg-ink p-6"
		onclick={(e) => e.stopPropagation()}
		role="dialog"
		aria-modal="true"
		tabindex="-1"
	>
		<div class="mb-4 flex items-center justify-between">
			<h3 class="text-xl">{title}</h3>
			{#if dismissable}
				<button class="text-paper/60 hover:text-accent" onclick={onClose} aria-label="Close">×</button>
			{/if}
		</div>
		{@render children?.()}
	</div>
</div>
