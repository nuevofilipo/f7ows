/**
 * Active-chat state: the message list for one chat, plus send + poll logic.
 *
 * Messages form a tree via their ltree `path`; we keep them as a flat list sorted
 * by path (the API already returns them in tree order) and build the nested view
 * in the component. Sending appends the user + assistant messages the API returns;
 * background commands (deepsummary) come back "generating" and are polled to ready.
 */
import { listMessages, sendMessage, listCommands } from '$lib/api/chats.js';

const READY_STATES = new Set([undefined, '', 'ready', 'error']);

export class ChatStore {
	/** @type {import('$lib/api/chats.js').Message[]} */
	messages = $state([]);
	/** @type {Awaited<ReturnType<typeof listCommands>>} */
	commands = $state([]);
	loading = $state(false);
	sending = $state(false);
	/** @type {string|null} */
	error = $state(null);

	/** @param {string} projectId @param {string} chatId */
	constructor(projectId, chatId) {
		this.projectId = projectId;
		this.chatId = chatId;
	}

	async load() {
		this.loading = true;
		this.error = null;
		try {
			const [messages, commands] = await Promise.all([
				listMessages(this.projectId, this.chatId),
				this.commands.length ? Promise.resolve(this.commands) : listCommands()
			]);
			this.messages = messages;
			this.commands = commands;
			// Resume polling anything still generating (e.g. after a refresh).
			for (const m of messages) if (isGenerating(m)) this.#poll(m.id);
		} catch (/** @type {any} */ err) {
			this.error = err?.message ?? 'Failed to load chat';
		} finally {
			this.loading = false;
		}
	}

	/**
	 * @param {Object} opts
	 * @param {string} opts.content
	 * @param {string} [opts.command]
	 * @param {string} [opts.parentPath]
	 * @param {string} [opts.intent]
	 * @param {number} [opts.level]  visual hierarchy level for the new turn (1–3)
	 * @param {string[]} [opts.sourceIds]  documents to scope retrieval to
	 */
	async send({ content, command, parentPath, intent, level = 1, sourceIds }) {
		this.sending = true;
		this.error = null;

		// Optimistic UI: show the user's message and a typing placeholder right
		// away (the backend nests the user turn under the parent, and the assistant
		// turn under the user turn — mirror that with temp paths). The placeholders
		// carry `level` so they render at the right indentation immediately.
		//
		// Dedupe by a UNIQUE TOKEN ID, never by object reference: Svelte's $state
		// deeply proxies what we store, so the proxy in `this.messages` is not ===
		// the original object — a reference filter would leave the placeholders
		// behind (lingering typing dots + a duplicated user message).
		const token = crypto.randomUUID();
		const effectiveParent = parentPath ?? this.messages.at(-1)?.path ?? null;
		const userTmpPath = effectiveParent ? `${effectiveParent}.tmp` : 'tmp';
		const tmpUser = makeTmp(`tmp-u-${token}`, this.chatId, userTmpPath, 'user', content, { level });
		const tmpAssistant = makeTmp(`tmp-a-${token}`, this.chatId, `${userTmpPath}.tmp`, 'assistant', '', {
			pending: true,
			level
		});
		this.messages = [...this.messages, tmpUser, tmpAssistant];
		/** @param {import('$lib/api/chats.js').Message[]} list */
		const drop = (list) => list.filter((m) => m.id !== tmpUser.id && m.id !== tmpAssistant.id);

		try {
			const res = await sendMessage(this.projectId, this.chatId, {
				content,
				level,
				...(command ? { command } : {}),
				...(parentPath ? { parent_path: parentPath } : {}),
				...(intent ? { intent } : {}),
				...(sourceIds?.length ? { source_ids: sourceIds } : {})
			});
			// Swap the placeholders for the real messages.
			this.messages = drop(this.messages).concat([res.user_message, res.assistant_message]);
			if (isGenerating(res.assistant_message)) this.#poll(res.assistant_message.id);
			return res;
		} catch (/** @type {any} */ err) {
			// Drop the optimistic placeholders on failure.
			this.messages = drop(this.messages);
			this.error = err?.message ?? 'Failed to send message';
			throw err;
		} finally {
			this.sending = false;
		}
	}

	/**
	 * Poll a generating (background-command) assistant message until it's ready,
	 * swapping the placeholder in place each time.
	 * @param {string} messageId
	 */
	async #poll(messageId) {
		for (let i = 0; i < 150; i++) {
			await sleep(2000);
			let fresh;
			try {
				fresh = await listMessages(this.projectId, this.chatId);
			} catch {
				continue; // transient; keep trying
			}
			const updated = fresh.find((m) => m.id === messageId);
			if (!updated) return;
			this.messages = this.messages.map((m) => (m.id === messageId ? updated : m));
			if (!isGenerating(updated)) return;
		}
	}
}

/** @param {import('$lib/api/chats.js').Message} m */
export function isGenerating(m) {
	return m.role === 'assistant' && !READY_STATES.has(m.metadata?.state);
}

/**
 * A message is "pending" while it's an optimistic placeholder awaiting the reply.
 * @param {import('$lib/api/chats.js').Message} m
 */
export function isPending(m) {
	return m?.metadata?.pending === true;
}

/**
 * Build an optimistic placeholder message. `id` is a unique temp token so it
 * never collides with a real (UUID) message and can be filtered by value.
 * @param {string} id @param {string} chatId @param {string} path
 * @param {'user'|'assistant'} role @param {string} content
 * @param {Record<string, any>} metadata
 */
function makeTmp(id, chatId, path, role, content, metadata) {
	return {
		id,
		chat_id: chatId,
		path,
		role,
		content,
		metadata,
		created_at: new Date().toISOString()
	};
}

/** @param {number} ms */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Build a nested tree from the flat, path-sorted message list. Each node is
 * `{ message, children: node[] }`; nesting follows the ltree `path` (a.b.c).
 * @param {import('$lib/api/chats.js').Message[]} messages
 */
export function buildTree(messages) {
	/** @type {Record<string, { message: import('$lib/api/chats.js').Message, children: any[] }>} */
	const byPath = {};
	const roots = [];
	for (const message of messages) byPath[message.path] = { message, children: [] };
	for (const message of messages) {
		const node = byPath[message.path];
		const parentPath = message.path.split('.').slice(0, -1).join('.');
		if (parentPath && byPath[parentPath]) byPath[parentPath].children.push(node);
		else roots.push(node);
	}
	return roots;
}
