<script>
	/**
	 * Minimal centered modal with a backdrop. Close on backdrop click or Esc.
	 * @type {{ title?: string, onClose: () => void, children?: import('svelte').Snippet }}
	 */
	let { title = '', onClose, children } = $props();

	/** @param {KeyboardEvent} e */
	function onKeydown(e) {
		if (e.key === 'Escape') onClose();
	}
</script>

<svelte:window onkeydown={onKeydown} />

<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
	onclick={onClose}
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
			<button class="text-paper/60 hover:text-accent" onclick={onClose} aria-label="Close">×</button>
		</div>
		{@render children?.()}
	</div>
</div>
