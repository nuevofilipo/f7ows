<script>
	/** @type {{ size?: number, label?: string }} */
	let { size = 16, label = '' } = $props();
</script>

<span class="inline-flex items-center gap-2 text-paper/70">
	<span
		class="inline-block animate-spin rounded-full border-2 border-paper/30 border-t-accent"
		style="width:{size}px;height:{size}px"
	></span>
	{#if label}<span class="text-sm">{label}</span>{/if}
</span>
