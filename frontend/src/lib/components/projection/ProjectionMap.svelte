<script>
	/**
	 * The map canvas — an SVG scatter of the project's chunks.
	 *
	 * Each chunk is a circle placed at its 2D coordinate for the selected
	 * projection method, sized by token_count and styled by study state:
	 *   actively_recalled -> filled accent, summarised -> accent outline,
	 *   not_studied -> paper outline (see the legend).
	 * Coordinates come pre-computed from the backend; we only fit the cloud to the
	 * available pixels. When a search is active, matched chunks are ringed and the
	 * rest are dimmed.
	 *
	 * @type {{
	 *   chunks: import('$lib/api/projection.js').ProjectionChunk[],
	 *   method: 'umap_2d'|'pca'|'tsne',
	 *   highlight: Set<string>,
	 *   searchActive: boolean
	 * }}
	 */
	let { chunks, method, highlight, searchActive } = $props();

	// Canvas pixel size, measured from the wrapping div (keeps circles round and
	// uses the full area rather than letterboxing a fixed viewBox).
	let w = $state(0);
	let h = $state(0);
	const PAD = 48; // keep edge points (and their radius) off the border

	/** @type {string|null} */
	let hoveredId = $state(null);

	// Only chunks that actually have a coordinate for the chosen method are plottable.
	// Pull x/y up front so downstream code never touches a possibly-null coord.
	const points = $derived(
		chunks
			.map((c) => ({ c, p: c.coords[method] }))
			.filter((e) => !!e.p && Number.isFinite(e.p.x) && Number.isFinite(e.p.y))
			.map((e) => ({ c: e.c, x: /** @type {number} */ (e.p?.x), y: /** @type {number} */ (e.p?.y) }))
	);

	/**
	 * Map a data range to a pixel range; collapse to the centre if the range is flat.
	 * @param {number} min @param {number} max @param {number} lo @param {number} hi
	 */
	function scaler(min, max, lo, hi) {
		const span = max - min;
		if (span < 1e-9) return () => (lo + hi) / 2;
		return (/** @type {number} */ v) => lo + ((v - min) / span) * (hi - lo);
	}

	const layout = $derived.by(() => {
		if (points.length === 0 || w === 0 || h === 0) return [];
		const xs = points.map((e) => e.x);
		const ys = points.map((e) => e.y);
		const ts = points.map((e) => e.c.token_count || 0);
		const sx = scaler(Math.min(...xs), Math.max(...xs), PAD, w - PAD);
		// Flip y so larger values sit higher, like a conventional plot.
		const sy = scaler(Math.min(...ys), Math.max(...ys), h - PAD, PAD);
		const tMin = Math.min(...ts);
		const tMax = Math.max(...ts);
		const sr = scaler(tMin, tMax, 7, 24);
		return points.map((e) => ({
			id: e.c.id,
			c: e.c,
			cx: sx(e.x),
			cy: sy(e.y),
			r: tMax - tMin < 1e-9 ? 12 : sr(e.c.token_count || 0)
		}));
	});

	const hovered = $derived(layout.find((d) => d.id === hoveredId)?.c ?? null);

	/** Fill/stroke per study state, on the shared design tokens. */
	function style(/** @type {string} */ study) {
		if (study === 'actively_recalled') return { fill: 'var(--color-accent)', stroke: 'var(--color-accent)' };
		if (study === 'summarised') return { fill: 'transparent', stroke: 'var(--color-accent)' };
		return { fill: 'transparent', stroke: 'var(--color-paper)' };
	}
</script>

<div class="relative h-full w-full" bind:clientWidth={w} bind:clientHeight={h}>
	{#if points.length === 0}
		<div class="absolute inset-0 flex items-center justify-center">
			<p class="text-sm text-paper/40">No points for this projection yet.</p>
		</div>
	{:else}
		<svg width={w} height={h} class="block" role="img" aria-label="Chunk projection map">
			{#each layout as d (d.id)}
				{@const s = style(d.c.study)}
				{@const isHit = highlight.has(d.id)}
				{@const dim = searchActive && !isHit}
				<g
					role="presentation"
					onpointerenter={() => (hoveredId = d.id)}
					onpointerleave={() => (hoveredId = null)}
				>
					<!-- Search-hit ring: a bright accent halo around matched points. -->
					{#if isHit}
						<circle cx={d.cx} cy={d.cy} r={d.r + 5} fill="none" stroke="var(--color-accent)" stroke-width="2" />
					{/if}
					<circle
						cx={d.cx}
						cy={d.cy}
						r={d.r}
						fill={s.fill}
						stroke={s.stroke}
						stroke-width="1.5"
						opacity={dim ? 0.18 : 1}
						class="cursor-pointer transition-opacity"
					/>
				</g>
			{/each}
		</svg>

		<!-- Hover label: the chunk's heading trail + source document. -->
		{#if hovered}
			<div class="pointer-events-none absolute left-3 top-3 max-w-[60%] border border-paper bg-ink/90 px-3 py-2 text-xs">
				<p class="truncate text-paper">
					{hovered.heading_trail?.length ? hovered.heading_trail.join(' › ') : (hovered.path ?? 'chunk')}
				</p>
				<p class="mt-0.5 truncate text-paper/50">{hovered.document_name} · {hovered.token_count} tok</p>
			</div>
		{/if}
	{/if}
</div>
