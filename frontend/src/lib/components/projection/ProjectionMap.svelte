<script>
  /**
   * The map canvas — an SVG scatter of the project's chunks.
   *
   * Each chunk is a circle placed at its 2D coordinate for the selected
   * projection method, sized by token_count and styled by study state:
   *   actively_studied -> filled accent, passively_studied -> accent outline,
   *   not_studied -> paper outline (see the legend).
   * Coordinates come pre-computed from the backend; we only fit the cloud to the
   * available pixels. When a search is active, matched chunks are ringed and the
   * rest are dimmed.
   *
   * Interaction:
   *   - Pan/zoom via d3-zoom. The transform is applied to a single wrapping <g>,
   *     so positions and radii scale together (overlap stays zoom-invariant) while
   *     stroke widths are divided by the zoom factor to stay constant on screen.
   *   - Optional declutter: a d3-force pass (forceCollide + a weak positional
   *     anchor) nudges overlapping circles apart until they touch but never
   *     overlap, preserving as much of the UMAP relative layout as possible.
   *
   * @type {{
   *   projectId: string,
   *   chunks: import('$lib/api/projection.js').ProjectionChunk[],
   *   method: 'umap_2d'|'pca'|'tsne',
   *   highlight: Set<string>,
   *   searchActive: boolean,
   *   declutter?: boolean,
   *   anchorStrength?: number
   * }}
   */
  let { projectId, chunks, method, highlight, searchActive, declutter = true, anchorStrength = 0.5 } = $props();

  import { zoom, zoomIdentity } from "d3-zoom";
  import { select } from "d3-selection";
  import { forceSimulation, forceCollide, forceX, forceY } from "d3-force";
  import { getProjectionChunk } from "$lib/api/projection.js";
  import Spinner from "$lib/components/ui/Spinner.svelte";

  // Canvas pixel size, measured from the wrapping div (keeps circles round and
  // uses the full area rather than letterboxing a fixed viewBox).
  let w = $state(0);
  let h = $state(0);
  const PAD = 48; // keep edge points (and their radius) off the border
  const GAP = 1; // breathing room between circles when decluttering (px)
  const TICKS = 220; // relaxation iterations — enough to settle at this scale

  /** @type {string|null} */
  let hoveredId = $state(null);

  // Click selection: the open chunk's id, its lazily-fetched content, and load state.
  /** @type {string|null} */
  let selectedId = $state(null);
  /** @type {string|null} */
  let selectedContent = $state(null);
  let contentLoading = $state(false);
  /** @type {string|null} */
  let contentError = $state(null);
  // Distinguishes a click from the tail of a pan: a pointerdown that travels more
  // than a few px before release is a drag, not a selection.
  /** @type {{ x: number, y: number }|null} */
  let downPt = null;

  // Geometry of the detail popup (top-left, below the hover label, capped above the
  // legend). Used only for the occlusion test + pan target, so the maths stays
  // stable as the box grows. Keep these in sync with the popup's CSS insets below.
  const BOX_W = 360; // matches the popup's w-[360px]
  const BOX_M = 12; // left-3 inset
  const BOX_TOP = 12; // top-3: anchored in the upper-left corner
  const BOX_BOTTOM = 112; // bottom-28: gap kept above the legend
  const VIEW_PAD = 24; // keep the focused point this far from any edge
  const COMFORT_GAP = 80; // when nudging a point clear, leave this much past the box

  // Pan/zoom transform, driven by d3-zoom.
  let t = $state({ x: 0, y: 0, k: 1 });
  /** @type {SVGSVGElement|undefined} */
  let svgEl = $state();
  /** @type {import('d3-zoom').ZoomBehavior<SVGSVGElement, unknown>|undefined} */
  let zoomBehavior;

  // Only chunks that actually have a coordinate for the chosen method are plottable.
  // Pull x/y up front so downstream code never touches a possibly-null coord.
  const points = $derived(
    chunks
      .map((c) => ({ c, p: c.coords[method] }))
      .filter((e) => !!e.p && Number.isFinite(e.p.x) && Number.isFinite(e.p.y))
      .map((e) => ({ c: e.c, x: /** @type {number} */ (e.p?.x), y: /** @type {number} */ (e.p?.y) })),
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

  // Raw fitted layout: data coords -> pixels, radius from token_count. This is the
  // "true" UMAP placement before any decluttering.
  const fitted = $derived.by(() => {
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
      r: tMax - tMin < 1e-9 ? 12 : sr(e.c.token_count || 0),
    }));
  });

  // Decluttered layout: relax overlaps with d3-force while anchoring each point to
  // its (pre-scaled) fitted position. We pre-scale the cloud inward first so that
  // the outward spread from collision lands roughly back inside the PAD box.
  const layout = $derived.by(() => {
    const base = fitted;
    if (!declutter || base.length < 2) return base;

    // Estimate how much the cloud must expand to fit non-overlapping circles, then
    // pre-shrink anchors by the inverse so the relaxed result fits the viewport.
    const cx0 = base.reduce((s, d) => s + d.cx, 0) / base.length;
    const cy0 = base.reduce((s, d) => s + d.cy, 0) / base.length;
    const xs = base.map((d) => d.cx);
    const ys = base.map((d) => d.cy);
    const cloudArea = Math.max(1, (Math.max(...xs) - Math.min(...xs)) * (Math.max(...ys) - Math.min(...ys)));
    const PACK = 0.7; // target fraction of area circles may occupy
    const circleArea = base.reduce((s, d) => s + Math.PI * (d.r + GAP) ** 2, 0);
    const expansion = Math.max(1, Math.sqrt(circleArea / PACK / cloudArea));
    const pre = 1 / expansion;

    // Map the UI 0..1 anchor strength onto a useful force range. Higher = stays
    // closer to the original UMAP shape; lower = more even, airier spacing.
    const k = 0.03 + 0.27 * Math.min(1, Math.max(0, anchorStrength));

    const nodes = base.map((d) => ({
      ...d,
      ax: cx0 + (d.cx - cx0) * pre,
      ay: cy0 + (d.cy - cy0) * pre,
      x: cx0 + (d.cx - cx0) * pre,
      y: cy0 + (d.cy - cy0) * pre,
    }));

    const sim = forceSimulation(nodes)
      .force("collide", forceCollide((/** @type {any} */ d) => d.r + GAP).strength(1))
      .force("x", forceX((/** @type {any} */ d) => d.ax).strength(k))
      .force("y", forceY((/** @type {any} */ d) => d.ay).strength(k))
      .stop();
    for (let i = 0; i < TICKS; i++) sim.tick();

    return nodes.map((n) => ({ id: n.id, c: n.c, cx: n.x, cy: n.y, r: n.r }));
  });

  const hoveredNode = $derived(layout.find((d) => d.id === hoveredId) ?? null);
  const hovered = $derived(hoveredNode?.c ?? null);

  // Where the hover label sits: next to the hovered point, clamped inside the canvas
  // and (when the detail box is open) kept to the right of it so it never overlaps.
  const hoverLabel = $derived.by(() => {
    if (!hoveredNode || w === 0 || h === 0) return null;
    const LABEL_W = 280; // matches the label's w-[280px] (so the clamp is accurate)
    const LABEL_H = 56;
    const OFF = 14; // gap between the point and the label
    const PAD = 8; // min gap to the canvas edge
    const sx = hoveredNode.cx * t.k + t.x;
    const sy = hoveredNode.cy * t.k + t.y;
    const minLeft = selectedId ? BOX_M + BOX_W + 12 : PAD;
    return {
      left: Math.min(Math.max(sx + OFF, minLeft), w - LABEL_W - PAD),
      top: Math.min(Math.max(sy + OFF, PAD), h - LABEL_H - PAD),
    };
  });

  // The selected point (with its layout coords) + its chunk metadata, for the popup
  // header and the pan-to-clear maths. Null if nothing is open or it has no coord
  // for the current method (e.g. the user switched projection while a popup was up).
  const selectedNode = $derived(layout.find((d) => d.id === selectedId) ?? null);
  const selected = $derived(selectedNode?.c ?? null);

  /** Open a chunk: ring it, nudge it clear of the popup, then lazy-load its text. */
  async function selectChunk(/** @type {string} */ id) {
    selectedId = id;
    selectedContent = null;
    contentError = null;
    contentLoading = true;
    panToClear(id);
    try {
      const res = await getProjectionChunk(projectId, id);
      if (selectedId === id) selectedContent = res.content_full;
    } catch (/** @type {any} */ err) {
      if (selectedId === id) contentError = err?.message ?? "Failed to load chunk";
    } finally {
      if (selectedId === id) contentLoading = false;
    }
  }

  function closeSelection() {
    selectedId = null;
    selectedContent = null;
    contentError = null;
    contentLoading = false;
  }

  /**
   * Pan (keeping the zoom level) so the selected point isn't hidden behind the
   * bottom-right popup. If it's already comfortably in view, do nothing — only a
   * genuinely occluded/off-screen point gets moved, into the top-left open area.
   */
  function panToClear(/** @type {string} */ id) {
    const node = layout.find((d) => d.id === id);
    if (!node || w === 0 || h === 0) return;
    const sx = node.cx * t.k + t.x; // current on-screen position
    const sy = node.cy * t.k + t.y;
    const boxRight = BOX_M + BOX_W;
    const boxBottom = h - BOX_BOTTOM;

    // Move as little as possible: clamp into the padded viewport, then — only if
    // the point still sits under the popup — slide it just past the box's right
    // edge. No recentring; a comfortable nudge is enough.
    let targetSx = Math.min(Math.max(sx, VIEW_PAD), w - VIEW_PAD);
    const targetSy = Math.min(Math.max(sy, VIEW_PAD), h - VIEW_PAD);
    const inBoxBand = targetSy > BOX_TOP && targetSy < boxBottom;
    if (inBoxBand && targetSx < boxRight + COMFORT_GAP) targetSx = boxRight + COMFORT_GAP;

    if (Math.abs(targetSx - sx) < 0.5 && Math.abs(targetSy - sy) < 0.5) return; // already clear
    animateTo(t.x + (targetSx - sx), t.y + (targetSy - sy), t.k);
  }

  /** @type {number} */
  let raf = 0;

  /**
   * Smoothly drive d3-zoom to a target transform. We step the zoom behaviour's own
   * `transform` each frame (rather than mutating `t` directly) so d3 keeps its
   * internal state in sync — otherwise the next user gesture would jump.
   */
  function animateTo(/** @type {number} */ tx, /** @type {number} */ ty, /** @type {number} */ k, duration = 400) {
    if (!svgEl || !zoomBehavior) return;
    const sel = select(svgEl);
    const zb = zoomBehavior;
    const s = { x: t.x, y: t.y, k: t.k };
    const t0 = performance.now();
    cancelAnimationFrame(raf);
    const frame = (/** @type {number} */ now) => {
      const e = Math.min(1, (now - t0) / duration);
      const a = 1 - Math.pow(1 - e, 3); // ease-out cubic
      const next = zoomIdentity.translate(s.x + (tx - s.x) * a, s.y + (ty - s.y) * a).scale(s.k + (k - s.k) * a);
      sel.call(zb.transform, next);
      if (e < 1) raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
  }

  // d3-zoom: attach once the <svg> exists; keep transform in $state for the template.
  $effect(() => {
    if (!svgEl) return;
    const sel = select(svgEl);
    const zb = /** @type {import('d3-zoom').ZoomBehavior<SVGSVGElement, unknown>} */ (zoom());
    zb.scaleExtent([0.3, 12]).on("zoom", (/** @type {any} */ e) => {
      t = { x: e.transform.x, y: e.transform.y, k: e.transform.k };
    });
    zoomBehavior = zb;
    sel.call(zb);
    return () => {
      sel.on(".zoom", null);
    };
  });

  /**
   * Reset by fitting the whole cloud into view. When the detail popup is open we
   * fit into the area to the right of it (the visible region); otherwise into the
   * entire window. Either way the cloud is scaled + centred to fill that target.
   */
  function resetView() {
    if (!svgEl || !zoomBehavior || layout.length === 0 || w === 0 || h === 0) return;

    // Bounding box of the cloud, including each circle's radius so nothing clips.
    let minX = Infinity,
      maxX = -Infinity,
      minY = Infinity,
      maxY = -Infinity;
    for (const d of layout) {
      minX = Math.min(minX, d.cx - d.r);
      maxX = Math.max(maxX, d.cx + d.r);
      minY = Math.min(minY, d.cy - d.r);
      maxY = Math.max(maxY, d.cy + d.r);
    }
    const dataW = Math.max(1e-6, maxX - minX);
    const dataH = Math.max(1e-6, maxY - minY);

    // Target rectangle: shrink past the popup's right edge when it's open.
    const left = (selectedId ? BOX_M + BOX_W : 0) + VIEW_PAD;
    const right = w - VIEW_PAD;
    const top = VIEW_PAD;
    const bottom = h - VIEW_PAD;

    let k = Math.min((right - left) / dataW, (bottom - top) / dataH);
    k = Math.min(12, Math.max(0.3, k)); // honour the zoom extent
    const tx = (left + right) / 2 - ((minX + maxX) / 2) * k;
    const ty = (top + bottom) / 2 - ((minY + maxY) / 2) * k;
    animateTo(tx, ty, k);
  }

  /** Fill/stroke per study state, on the shared design tokens. */
  function style(/** @type {string} */ study) {
    if (study === "actively_studied") return { fill: "var(--color-accent)", stroke: "var(--color-accent)" };
    if (study === "passively_studied") return { fill: "transparent", stroke: "var(--color-accent)" };
    return { fill: "transparent", stroke: "var(--color-paper)" };
  }
</script>

<div class="relative h-full w-full" bind:clientWidth={w} bind:clientHeight={h}>
  {#if points.length === 0}
    <div class="absolute inset-0 flex items-center justify-center">
      <p class="text-sm text-paper/40">No points for this projection yet.</p>
    </div>
  {:else}
    <svg bind:this={svgEl} width={w} height={h} class="block touch-none" role="img" aria-label="Chunk projection map">
      <g transform="translate({t.x},{t.y}) scale({t.k})">
        {#each layout as d (d.id)}
          {@const s = style(d.c.study)}
          {@const isHit = highlight.has(d.id)}
          {@const isSelected = d.id === selectedId}
          {@const dim = searchActive && !isHit && !isSelected}
          <g
            role="presentation"
            onpointerenter={() => (hoveredId = d.id)}
            onpointerleave={() => (hoveredId = null)}
            onpointerdown={(e) => (downPt = { x: e.clientX, y: e.clientY })}
            onclick={(e) => {
              if (downPt && Math.hypot(e.clientX - downPt.x, e.clientY - downPt.y) < 5) selectChunk(d.id);
            }}
          >
            <!-- Search-hit ring: a bright accent halo around matched points. -->
            {#if isHit}
              <circle cx={d.cx} cy={d.cy} r={d.r + 5} fill="none" stroke="var(--color-accent)" stroke-width={2 / t.k} />
            {/if}
            <!-- Selected ring: a thicker double halo, distinct from a search hit. -->
            {#if isSelected}
              <circle cx={d.cx} cy={d.cy} r={d.r + 8} fill="none" stroke="var(--color-accent)" stroke-width={3 / t.k} />
            {/if}
            <circle
              cx={d.cx}
              cy={d.cy}
              r={d.r}
              fill={s.fill}
              stroke={s.stroke}
              stroke-width={1.5 / t.k}
              opacity={dim ? 0.18 : 1}
              class="cursor-pointer transition-opacity"
            />
          </g>
        {/each}
      </g>
    </svg>

    <!-- Reset view: only shown once the user has panned/zoomed away from identity. -->
    {#if t.k !== 1 || t.x !== 0 || t.y !== 0}
      <button type="button" class="absolute right-3 top-3 border border-paper bg-ink/90 px-2 py-1 text-xs hover:border-accent" onclick={resetView}>
        reset view
      </button>
    {/if}

    <!-- Hover label: the chunk's heading trail + source document, placed next to the
         hovered point (clamped inside the canvas, clear of the detail box). -->
    {#if hovered && hoverLabel}
      <div
        class="pointer-events-none absolute w-[280px] border border-paper bg-ink px-3 py-2 text-xs"
        style="left:{hoverLabel.left}px; top:{hoverLabel.top}px"
      >
        <p class="truncate text-paper">
          {hovered.heading_trail?.length ? hovered.heading_trail.join(" › ") : (hovered.path ?? "chunk")}
        </p>
        <p class="mt-0.5 truncate text-paper/50">{hovered.document_name} · {hovered.token_count} tok</p>
      </div>
    {/if}

    <!-- Detail popup: the clicked chunk's full content, anchored top-left and
		     growing down to a gap above the legend (the view pans to keep the
		     selected point clear of this box). -->
    {#if selectedId}
      <div class="scroll-clean absolute bottom-28 left-3 top-3 flex w-[360px] flex-col overflow-hidden border border-paper bg-ink">
        <div class="flex items-start gap-2 border-b border-paper px-3 py-2">
          <div class="min-w-0 flex-1">
            <p class="truncate text-xs text-paper">
              {selected?.heading_trail?.length ? selected.heading_trail.join(" › ") : (selected?.path ?? "chunk")}
            </p>
            {#if selected}
              <p class="mt-0.5 truncate text-xs text-paper/50">
                {selected.document_name} · {selected.token_count} tok
              </p>
            {/if}
          </div>
          <button type="button" class="shrink-0 text-paper/60 hover:text-accent" aria-label="Close chunk detail" onclick={closeSelection}> ✕ </button>
        </div>
        <div class="scroll-clean min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {#if contentLoading}
            <Spinner size={20} label="loading…" />
          {:else if contentError}
            <p class="text-xs text-red-400">{contentError}</p>
          {:else if selectedContent}
            <div class="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-paper/80">
              {selectedContent}
            </div>
          {/if}
        </div>
      </div>
    {/if}
  {/if}
</div>

<svelte:window onkeydown={(e) => e.key === "Escape" && selectedId && closeSelection()} />
