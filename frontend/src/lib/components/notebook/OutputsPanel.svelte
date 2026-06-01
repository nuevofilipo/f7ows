<script>
	/**
	 * Right column — "secondary chat" / "outputs" tabs (per Figma). Both are
	 * stubbed for this pass; the chat core is the functional focus. Outputs
	 * (quiz, Anki .apkg, source download) and secondary chat land in a later pass.
	 */
	/** @type {'secondary' | 'outputs'} */
	let tab = $state('outputs');
</script>

<section class="flex h-full min-h-0 flex-col border-y border-r border-paper">
	<header class="flex items-center justify-center gap-5 border-b border-paper px-4 py-3 text-[18px]">
		<button class:underline={tab === 'secondary'} class="text-paper" onclick={() => (tab = 'secondary')}>
			secondary chat
		</button>
		<button class:underline={tab === 'outputs'} class="text-paper" onclick={() => (tab = 'outputs')}>
			outputs
		</button>
	</header>

	<div class="scroll-clean min-h-0 flex-1 overflow-y-auto px-4 py-3">
		{#if tab === 'outputs'}
			<p class="text-sm text-paper/40">
				Quiz, Anki flashcards (.apkg) and source downloads will appear here.
			</p>
		{:else}
			<p class="text-sm text-paper/40">A separate side conversation — coming soon.</p>
		{/if}
	</div>
</section>
