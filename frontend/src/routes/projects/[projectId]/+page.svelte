<script>
	import { page } from '$app/state';
	import { listProjects } from '$lib/api/projects.js';
	import { listChats } from '$lib/api/chats.js';
	import { session } from '$lib/stores/session.svelte.js';
	import NotebookShell from '$lib/components/notebook/NotebookShell.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	const projectId = $derived(page.params.projectId ?? '');

	/** localStorage key for the last chat the user had open in a given project. */
	const lastChatKey = (/** @type {string} */ pid) => `notebook:lastChat:${pid}`;

	let projectName = $state('Notebook');
	/** @type {import('$lib/api/chats.js').ChatSummary[]} */
	let chats = $state([]);
	/** @type {string} */
	let chatId = $state('');
	let loading = $state(true);
	/** @type {string|null} */
	let error = $state(null);

	$effect(() => {
		if (!session.authenticated || !projectId) return;
		loading = true;
		(async () => {
			try {
				// No "get one project" endpoint — find its name in the list.
				const [projects, chatList] = await Promise.all([listProjects(), listChats(projectId)]);
				projectName = projects.find((p) => p.id === projectId)?.name ?? 'Notebook';
				// Most-recent-first, so the freshest chat is the natural default.
				chats = [...chatList].sort((a, b) =>
					(b.last_activity ?? '').localeCompare(a.last_activity ?? '')
				);
				// Reopen the chat the user last had open here (if it still exists),
				// else the most recent, else start a fresh client-generated one.
				const remembered = localStorage.getItem(lastChatKey(projectId));
				const exists = remembered && chats.some((c) => c.chat_id === remembered);
				chatId = (exists && remembered) || chats[0]?.chat_id || crypto.randomUUID();
			} catch (/** @type {any} */ err) {
				error = err?.message ?? 'Failed to load notebook';
			} finally {
				loading = false;
			}
		})();
	});

	// Remember the open chat per project so a reload reopens it (effects are
	// browser-only, so localStorage is safe here).
	$effect(() => {
		if (chatId && projectId) localStorage.setItem(lastChatKey(projectId), chatId);
	});

	/** @param {string} id */
	function selectChat(id) {
		chatId = id;
	}

	function newChat() {
		chatId = crypto.randomUUID();
	}
</script>

{#if loading}
	<div class="flex h-screen items-center justify-center">
		<Spinner size={28} label="loading notebook…" />
	</div>
{:else if error}
	<div class="flex h-screen items-center justify-center">
		<p class="text-red-400">{error}</p>
	</div>
{:else}
	<NotebookShell
		{projectName}
		{projectId}
		{chatId}
		{chats}
		onSelectChat={selectChat}
		onNewChat={newChat}
	/>
{/if}
