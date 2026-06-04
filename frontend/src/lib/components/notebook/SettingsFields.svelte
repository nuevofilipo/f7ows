<script>
	import ModelSelect from './ModelSelect.svelte';

	/**
	 * Schema-driven settings form fields. Renders one labelled control per schema
	 * entry and writes straight into the shared `values` object (a $state proxy, so
	 * mutating `values[key]` here is reactive in the parent). Used by both the chat
	 * settings modal and the profile page so the rendering lives in one place.
	 * @type {{
	 *   schema: import('$lib/api/settings.js').SettingsPayload['schema'],
	 *   values: Record<string, any>
	 * }}
	 */
	let { schema, values } = $props();
</script>

<div class="flex flex-col gap-4">
	{#each schema as f (f.key)}
		{#if f.type === 'bool'}
			<label class="flex items-center gap-2">
				<input type="checkbox" bind:checked={values[f.key]} class="accent-accent" />
				<span class="flex flex-col">
					<span class="text-sm text-paper">{f.key}</span>
					<span class="text-xs text-paper/50">{f.description}</span>
				</span>
			</label>
		{:else}
			<label class="flex flex-col gap-1">
				<span class="text-sm text-paper">{f.key}</span>
				<span class="text-xs text-paper/50">{f.description}</span>
				{#if f.key === 'chat_model'}
					<!-- Searchable picker over the gateway's models so the user never has
					     to type an exact model id. Fetches the list only when opened. -->
					<ModelSelect value={values[f.key]} onChange={(id) => (values[f.key] = id)} />
				{:else if f.choices}
					<select bind:value={values[f.key]} class="border border-paper bg-ink px-2 py-1">
						{#each f.choices as c (c)}<option value={c}>{c}</option>{/each}
					</select>
				{:else if f.key === 'system_prompt'}
					<textarea bind:value={values[f.key]} rows="5" class="scroll-clean border border-paper bg-ink px-2 py-1 text-sm"></textarea>
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
		{/if}
	{/each}
</div>
