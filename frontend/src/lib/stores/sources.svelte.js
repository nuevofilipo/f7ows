/**
 * Source-selection state — which documents the chat is scoped to.
 *
 * The left SourcesPanel owns the document list, but the ChatPanel/Composer (a
 * sibling, outside the `{#key chatId}` block) needs to know the selection when
 * sending. So the selection lives here as a singleton rune store: SourcesPanel
 * writes it, the chat reads `selectedIds` into the send payload. Sticky across
 * chat switches; re-seeded per project from the document list.
 *
 * Default behaviour (per product decision): ready sources start all-selected, and
 * a newly-ready document auto-selects — but a manual deselect is always honoured.
 */
import { SvelteSet } from 'svelte/reactivity';

class SourcesStore {
	/** Selected document ids. SvelteSet so reads in components stay reactive. */
	selected = new SvelteSet();
	/** Project these ids belong to; switching projects resets the selection. */
	#projectId = /** @type {string | null} */ (null);
	/** Every ready id we've already offered, so we only auto-select genuinely new ones. */
	#known = new Set();

	get selectedIds() {
		return [...this.selected];
	}

	get hasSelection() {
		return this.selected.size > 0;
	}

	/** @param {string} id */
	toggle(id) {
		if (this.selected.has(id)) this.selected.delete(id);
		else this.selected.add(id);
	}

	/** @param {string[]} ids */
	selectAll(ids) {
		for (const id of ids) this.selected.add(id);
	}

	clear() {
		this.selected.clear();
	}

	/**
	 * Reconcile the selection with the current document list. Auto-selects any
	 * newly-ready document (first time we see it ready), prunes ids for documents
	 * that are gone, and resets everything when the project changes.
	 * @param {string} projectId
	 * @param {{ id: string, ready: boolean }[]} docs
	 */
	sync(projectId, docs) {
		if (projectId !== this.#projectId) {
			this.#projectId = projectId;
			this.selected.clear();
			this.#known.clear();
		}
		const present = new Set(docs.map((d) => d.id));
		// Drop selections for documents that no longer exist.
		for (const id of this.selected) if (!present.has(id)) this.selected.delete(id);
		for (const id of this.#known) if (!present.has(id)) this.#known.delete(id);
		// Auto-select each ready document the first time we see it ready.
		for (const d of docs) {
			if (d.ready && !this.#known.has(d.id)) {
				this.#known.add(d.id);
				this.selected.add(d.id);
			}
		}
	}
}

export const sources = new SourcesStore();
