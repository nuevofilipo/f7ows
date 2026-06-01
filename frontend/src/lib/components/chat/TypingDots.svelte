<script>
	/**
	 * Three-dot "assistant is typing" indicator, shown top-left in the space the
	 * reply will fill while we wait for the grounded answer.
	 */
</script>

<div class="flex items-center gap-1.5 py-1" aria-label="Assistant is typing" role="status">
	<span class="dot"></span>
	<span class="dot"></span>
	<span class="dot"></span>
</div>

<style>
	.dot {
		width: 6px;
		height: 6px;
		border-radius: 9999px;
		background-color: var(--color-paper);
		animation: typing 1.2s infinite ease-in-out;
	}
	.dot:nth-child(2) {
		animation-delay: 0.18s;
	}
	.dot:nth-child(3) {
		animation-delay: 0.36s;
	}
	@keyframes typing {
		0%,
		60%,
		100% {
			transform: translateY(0);
			opacity: 0.35;
		}
		30% {
			transform: translateY(-4px);
			opacity: 0.9;
		}
	}
</style>
