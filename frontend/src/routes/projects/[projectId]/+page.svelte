<script>
	import { page } from '$app/state';
	import { listProjects } from '$lib/api/projects.js';
	import { listChats } from '$lib/api/chats.js';
	import { session } from '$lib/stores/session.svelte.js';
	import NotebookShell from '$lib/components/notebook/NotebookShell.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	const projectId = $derived(page.params.projectId ?? '');

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
				chats = chatList;
				// Open the most recent chat, or start a fresh client-generated one.
				chatId = chatList[0]?.chat_id ?? crypto.randomUUID();
			} catch (/** @type {any} */ err) {
				error = err?.message ?? 'Failed to load notebook';
			} finally {
				loading = false;
			}
		})();
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
