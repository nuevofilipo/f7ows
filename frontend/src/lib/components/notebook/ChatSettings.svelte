<script>
	import { getSettings, updateSettings } from '$lib/api/settings.js';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/**
	 * Chat Settings modal — schema-driven form over the user's preferences
	 * (model, temperature, system prompt, retrieval knobs). Full transparency of
	 * the system prompt is a PRD goal, so it's editable here.
	 * @type {{ onClose: () => void }}
	 */
	let { onClose } = $props();

	/** @type {import('$lib/api/settings.js').SettingsPayload | null} */
	let payload = $state(null);
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
		/** @type {Record<string, any>} */
		const patch = {};
		for (const f of payload.schema) {
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
			{#each payload.schema as f (f.key)}
				<label class="flex flex-col gap-1">
					<span class="text-sm text-paper">{f.key}</span>
					<span class="text-xs text-paper/50">{f.description}</span>
					{#if f.choices}
						<select bind:value={values[f.key]} class="border border-paper bg-ink px-2 py-1">
							{#each f.choices as c}<option value={c}>{c}</option>{/each}
						</select>
					{:else if f.key === 'system_prompt'}
						<textarea bind:value={values[f.key]} rows="5" class="border border-paper bg-ink px-2 py-1 text-sm"></textarea>
					{:else if f.type === 'int' || f.type === 'float'}
						<input
							type="number"
							bind:value={values[f.key]}
							min={f.min ?? undefined}
							max={f.max ?? undefined}
							step={f.type === 'float' ? '0.1' : '1'}
							class="border border-paper bg-ink px-2 py-1"
						/>
					{:else}
						<input type="text" bind:value={values[f.key]} class="border border-paper bg-ink px-2 py-1" />
					{/if}
				</label>
			{/each}
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
