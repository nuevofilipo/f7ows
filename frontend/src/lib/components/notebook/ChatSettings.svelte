<script>
	import { getSettings, updateSettings } from '$lib/api/settings.js';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import SettingsFields from './SettingsFields.svelte';

	/**
	 * Chat Settings modal — schema-driven form over the user's preferences
	 * (model, temperature, system prompt, retrieval knobs). Full transparency of
	 * the system prompt is a PRD goal, so it's editable here.
	 * @type {{ onClose: () => void }}
	 */
	let { onClose } = $props();

	/** @type {import('$lib/api/settings.js').SettingsPayload | null} */
	let payload = $state(null);
	// This modal only edits chat-group settings; projection knobs live on the map
	// page and appearance (accent colour) lives on the profile page.
	const chatSchema = $derived(
		/** @type {import('$lib/api/settings.js').SettingsPayload | null} */ (payload)?.schema.filter(
			(f) => f.group === 'chat'
		) ?? []
	);
	/** @type {Record<string, any>} */
	let values = $state({});
	let loading = $state(true);
	let saving = $state(false);
	/** @type {string|null} */
	let error = $state(null);

	$effect(() => {
		(async () => {
			try {
				const p = await getSettings();
				payload = p;
				values = { ...p.effective };
			} catch (/** @type {any} */ err) {
				error = err?.message ?? 'Failed to load settings';
			} finally {
				loading = false;
			}
		})();
	});

	async function save() {
		if (!payload) return;
		saving = true;
		error = null;
		// Only send fields that differ from the effective values (a small patch).
		// Projection-group settings are managed on the projection page, not here.
		/** @type {Record<string, any>} */
		const patch = {};
		for (const f of chatSchema) {
			if (values[f.key] !== payload.effective[f.key]) patch[f.key] = values[f.key];
		}
		try {
			const updated = await updateSettings(patch);
			payload = updated;
			values = { ...updated.effective };
			onClose();
		} catch (/** @type {any} */ err) {
			error = err?.message ?? 'Failed to save settings';
		} finally {
			saving = false;
		}
	}
</script>

<Modal title="Chat Settings" {onClose}>
	{#if loading}
		<Spinner label="loading…" />
	{:else if payload}
		<div class="flex flex-col gap-4">
			<SettingsFields schema={chatSchema} {values} />
			{#if error}<p class="text-sm text-red-400">{error}</p>{/if}
			<div class="flex justify-end gap-2">
				<Button variant="outline" onclick={onClose}>Cancel</Button>
				<Button onclick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
			</div>
		</div>
	{:else if error}
		<p class="text-sm text-red-400">{error}</p>
	{/if}
</Modal>
