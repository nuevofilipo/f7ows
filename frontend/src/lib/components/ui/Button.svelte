<script>
	/**
	 * Rectangular button matching the Figma (sharp corners, pink accent or
	 * outline). Forwards click and arbitrary attributes.
	 * @type {{
	 *   variant?: 'accent' | 'outline',
	 *   type?: 'button' | 'submit',
	 *   disabled?: boolean,
	 *   onclick?: (e: MouseEvent) => void,
	 *   class?: string,
	 *   children?: import('svelte').Snippet
	 * } & Record<string, any>}
	 */
	let {
		variant = 'accent',
		type = 'button',
		disabled = false,
		onclick,
		class: klass = '',
		children,
		...rest
	} = $props();

	const base = 'inline-flex items-center justify-center px-5 py-2 font-sans transition-opacity disabled:opacity-40 disabled:cursor-not-allowed';
	const variants = {
		accent: 'bg-accent text-paper hover:opacity-90',
		outline: 'border border-paper text-paper hover:bg-paper/10'
	};
</script>

<button {type} {disabled} {onclick} class="{base} {variants[variant]} {klass}" {...rest}>
	{@render children?.()}
</button>
