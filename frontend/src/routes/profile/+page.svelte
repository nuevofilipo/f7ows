<script>
	import {
		getSettings,
		updateSettings,
		setOpenRouterKey,
		clearOpenRouterKey
	} from '$lib/api/settings.js';
	import { session } from '$lib/stores/session.svelte.js';
	import { ACCENT_PRESETS, DEFAULT_ACCENT, applyAccent, setAccent } from '$lib/utils/theme.js';
	import SettingsFields from '$lib/components/notebook/SettingsFields.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	/** @type {import('$lib/api/settings.js').SettingsPayload | null} */
	let payload = $state(null);
	/** @type {Record<string, any>} */
	let values = $state({});
	let loading = $state(true);
	let saving = $state(false);
	/** @type {string|null} */
	let error = $state(null);
	let savedFlash = $state(false);

	// OpenRouter key — write-only: we show whether one is set + a masked preview,
	// and let the user replace or clear it. The plaintext is never read back.
	let keySet = $state(false);
	/** @type {string|null} */
	let keyPreview = $state(null);
	let keyInput = $state('');
	let keySaving = $state(false);
	/** @type {string|null} */
	let keyError = $state(null);

	// The chat-group settings restated here (same fields as the chat modal).
	const chatSchema = $derived(
		/** @type {import('$lib/api/settings.js').SettingsPayload | null} */ (payload)?.schema.filter(
			(f) => f.group === 'chat'
		) ?? []
	);
	const accent = $derived(values.accent_color ?? DEFAULT_ACCENT);

	$effect(() => {
		(async () => {
			try {
				const p = await getSettings();
				payload = p;
				values = { ...p.effective };
				keySet = p.openrouter_key_set;
				keyPreview = p.openrouter_key_preview;
				// Sync the saved accent to localStorage + the document on load.
				setAccent(values.accent_color ?? DEFAULT_ACCENT);
			} catch (/** @type {any} */ err) {
				error = err?.message ?? 'Failed to load profile';
			} finally {
				loading = false;
			}
		})();
	});

	/** Live-preview an accent without persisting (commit happens on Save). @param {string} hex */
	function pickAccent(hex) {
		values.accent_color = hex;
		applyAccent(hex);
	}

	async function save() {
		if (!payload) return;
		saving = true;
		error = null;
		// Send only changed chat + appearance fields (a small patch); projection
		// knobs are managed on the map page, not here.
		/** @type {Record<string, any>} */
		const patch = {};
		for (const f of payload.schema) {
			if (f.group === 'projection') continue;
			if (values[f.key] !== payload.effective[f.key]) patch[f.key] = values[f.key];
		}
		try {
			const updated = await updateSettings(patch);
			payload = updated;
			values = { ...updated.effective };
			setAccent(values.accent_color ?? DEFAULT_ACCENT);
			savedFlash = true;
			setTimeout(() => (savedFlash = false), 1500);
		} catch (/** @type {any} */ err) {
			error = err?.message ?? 'Failed to save settings';
		} finally {
			saving = false;
		}
	}

	async function saveKey() {
		const key = keyInput.trim();
		if (!key || keySaving) return;
		keySaving = true;
		keyError = null;
		try {
			const status = await setOpenRouterKey(key);
			keySet = status.openrouter_key_set;
			keyPreview = status.openrouter_key_preview;
			keyInput = '';
		} catch (/** @type {any} */ err) {
			keyError = err?.detail ?? err?.message ?? 'Failed to save key';
		} finally {
			keySaving = false;
		}
	}

	async function clearKey() {
		if (keySaving) return;
		keySaving = true;
		keyError = null;
		try {
			const status = await clearOpenRouterKey();
			keySet = status.openrouter_key_set;
			keyPreview = status.openrouter_key_preview;
		} catch (/** @type {any} */ err) {
			keyError = err?.detail ?? err?.message ?? 'Failed to clear key';
		} finally {
			keySaving = false;
		}
	}
</script>

<div class="mx-auto max-w-3xl px-6 py-12">
	<header class="mb-10 flex items-baseline justify-between">
		<h1 class="text-[32px]">My Profile</h1>
		<a href="/" class="text-sm text-paper/60 hover:text-accent">← Notebooks</a>
	</header>

	{#if loading}
		<Spinner label="loading…" />
	{:else if error && !payload}
		<p class="text-sm text-red-400">{error}</p>
	{:else}
		<p class="mb-10 text-sm text-paper/50">{session.user?.email}</p>

		<!-- Chat settings (System Prompt + model/sampling/retrieval knobs) -->
		<section class="mb-12">
			<h2 class="mb-4 text-[20px]">Chat settings</h2>
			<SettingsFields schema={chatSchema} {values} />
		</section>

		<!-- Colour theme -->
		<section class="mb-12">
			<h2 class="mb-4 text-[20px]">Colour theme</h2>
			<div class="flex items-center gap-4">
				<div class="flex gap-2">
					{#each ACCENT_PRESETS as preset (preset)}
						<button
							type="button"
							onclick={() => pickAccent(preset)}
							aria-label={`Use ${preset}`}
							aria-pressed={accent.toLowerCase() === preset.toLowerCase()}
							class="h-7 w-7 border transition-transform hover:scale-110 {accent.toLowerCase() ===
							preset.toLowerCase()
								? 'border-paper'
								: 'border-transparent'}"
							style="background-color: {preset}"
						></button>
					{/each}
				</div>

				<span class="text-paper/30">|</span>

				<!-- Custom picker: native swatch + editable hex, kept in sync. -->
				<label class="flex items-center gap-2">
					<span class="sr-only">Custom colour</span>
					<input
						type="color"
						value={accent}
						oninput={(e) => pickAccent(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
						class="h-7 w-9 cursor-pointer border border-paper bg-ink p-0.5"
					/>
					<input
						type="text"
						value={accent}
						oninput={(e) => pickAccent(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
						spellcheck="false"
						class="w-24 border border-paper bg-ink px-2 py-1 text-sm"
					/>
				</label>
			</div>
		</section>

		<!-- Save bar for chat settings + theme -->
		<div class="mb-12 flex items-center gap-3">
			<Button onclick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</Button>
			{#if savedFlash}<span class="text-sm text-paper/50">Saved.</span>{/if}
			{#if error && payload}<span class="text-sm text-red-400">{error}</span>{/if}
		</div>

		<!-- OpenRouter key (BYOK) — stored encrypted; only a masked preview comes back. -->
		<section class="mb-4 border-t border-paper/20 pt-8">
			<h2 class="mb-2 text-[20px]">OpenRouter key</h2>
			<p class="mb-4 max-w-xl text-sm text-paper/50">
				Bring your own OpenRouter key — when set, all your model traffic (chat, planner and
				embeddings) uses it. Stored encrypted; we never show it again after saving.
			</p>

			<p class="mb-3 text-sm">
				Status:
				{#if keySet}
					<span class="text-paper">set</span>
					<span class="text-paper/50">({keyPreview})</span>
				{:else}
					<span class="text-paper/50">not set — using the app's shared key</span>
				{/if}
			</p>

			<form
				class="flex max-w-xl gap-2"
				onsubmit={(e) => {
					e.preventDefault();
					saveKey();
				}}
			>
				<input
					type="password"
					bind:value={keyInput}
					placeholder={keySet ? 'Replace key (sk-or-…)' : 'sk-or-…'}
					autocomplete="off"
					spellcheck="false"
					class="flex-1 border border-paper bg-ink px-3 py-2 text-sm focus:outline-none"
				/>
				<Button type="submit" disabled={keySaving || !keyInput.trim()}>
					{keySaving ? '…' : keySet ? 'Replace' : 'Save'}
				</Button>
				{#if keySet}
					<Button variant="outline" onclick={clearKey} disabled={keySaving}>Clear</Button>
				{/if}
			</form>
			{#if keyError}<p class="mt-2 text-sm text-red-400">{keyError}</p>{/if}
		</section>
	{/if}
</div>
