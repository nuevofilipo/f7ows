<script>
	import { goto } from '$app/navigation';
	import { session } from '$lib/stores/session.svelte.js';
	import SourcesPanel from './SourcesPanel.svelte';
	import ChatPanel from './ChatPanel.svelte';
	import OutputsPanel from './OutputsPanel.svelte';
	import ChatSettings from './ChatSettings.svelte';

	/**
	 * Notebook page layout — top bar + 3 panes (Sources | Main Chat | Outputs).
	 * The panes sit flush so each adjacent edge is a single shared 1px border, and
	 * the two seams are draggable (within sane min/max) to resize the side panels.
	 * @type {{
	 *   projectName: string,
	 *   projectId: string,
	 *   chatId: string,
	 *   chats: import('$lib/api/chats.js').ChatSummary[],
	 *   onSelectChat: (id: string) => void,
	 *   onNewChat: () => void
	 * }}
	 */
	let { projectName, projectId, chatId, chats, onSelectChat, onNewChat } = $props();

	let settingsOpen = $state(false);
	let profileOpen = $state(false);

	// Resizable side panels (px). Clamped so the chat always keeps room.
	let sourcesWidth = $state(340);
	let outputsWidth = $state(356);
	const MIN = 220;
	const MAX = 560;

	/**
	 * Drag a seam to resize the adjacent side panel. Pointer capture keeps the
	 * drag alive even when the cursor leaves the thin handle.
	 * @param {'sources'|'outputs'} which @param {PointerEvent} e
	 */
	function startDrag(which, e) {
		e.preventDefault();
		const startX = e.clientX;
		const startW = which === 'sources' ? sourcesWidth : outputsWidth;
		const handle = /** @type {HTMLElement} */ (e.currentTarget);
		handle.setPointerCapture(e.pointerId);

		/** @param {PointerEvent} ev */
		const move = (ev) => {
			const delta = ev.clientX - startX;
			// Sources grows as you drag right; Outputs grows as you drag left.
			const next = which === 'sources' ? startW + delta : startW - delta;
			const clamped = Math.max(MIN, Math.min(MAX, next));
			if (which === 'sources') sourcesWidth = clamped;
			else outputsWidth = clamped;
		};
		const up = () => {
			handle.releasePointerCapture(e.pointerId);
			handle.removeEventListener('pointermove', move);
			handle.removeEventListener('pointerup', up);
		};
		handle.addEventListener('pointermove', move);
		handle.addEventListener('pointerup', up);
	}

	async function logout() {
		await session.logout();
		goto('/login');
	}
</script>

<div class="flex h-screen flex-col bg-ink p-4">
	<!-- Top bar -->
	<header class="flex items-center justify-between pb-3">
		<h1 class="text-[28px]">{projectName}</h1>
		<nav class="flex items-center gap-6 text-[20px]">
			<button class="text-paper/40" title="Projection page — coming soon" disabled>Projection</button>
			<div class="relative">
				<button onclick={() => (profileOpen = !profileOpen)}>My Profile</button>
				{#if profileOpen}
					<div class="absolute right-0 z-50 mt-2 w-56 border border-paper bg-ink p-3 text-sm">
						<p class="truncate text-paper/70">{session.user?.email}</p>
						<button class="mt-2 w-full bg-accent px-3 py-1 text-paper hover:opacity-90" onclick={logout}>Log out</button>
					</div>
				{/if}
			</div>
		</nav>
	</header>

	<!-- Panes: flush so each shared edge is a single 1px border; seams drag to resize. -->
	<div class="flex min-h-0 flex-1">
		<div class="shrink-0" style="width: {sourcesWidth}px">
			<SourcesPanel {projectId} />
		</div>

		<div
			class="relative z-10 -mx-0.5 w-1 shrink-0 cursor-col-resize select-none touch-none hover:bg-accent"
			onpointerdown={(e) => startDrag('sources', e)}
			role="separator"
			aria-orientation="vertical"
			aria-label="Resize sources panel"
		></div>

		<div class="flex min-h-0 min-w-0 flex-1 flex-col">
			{#key chatId}
				<ChatPanel
					{projectId}
					{chatId}
					{chats}
					{onSelectChat}
					{onNewChat}
					onOpenSettings={() => (settingsOpen = true)}
				/>
			{/key}
		</div>

		<div
			class="relative z-10 -mx-0.5 w-1 shrink-0 cursor-col-resize select-none touch-none hover:bg-accent"
			onpointerdown={(e) => startDrag('outputs', e)}
			role="separator"
			aria-orientation="vertical"
			aria-label="Resize outputs panel"
		></div>

		<div class="shrink-0" style="width: {outputsWidth}px">
			<OutputsPanel />
		</div>
	</div>
</div>

{#if settingsOpen}
	<ChatSettings onClose={() => (settingsOpen = false)} />
{/if}
