<script>
	import { goto } from '$app/navigation';
	import { session } from '$lib/stores/session.svelte.js';
	import Button from '$lib/components/ui/Button.svelte';

	let email = $state('');
	let password = $state('');
	/** @type {'login' | 'signup'} */
	let mode = $state('login');
	let submitting = $state(false);
	/** @type {string|null} */
	let error = $state(null);

	async function submit() {
		if (submitting) return;
		error = null;
		submitting = true;
		try {
			if (mode === 'login') await session.login(email.trim(), password);
			else await session.register(email.trim(), password);
			goto('/');
		} catch (/** @type {any} */ err) {
			error = err?.detail ?? err?.message ?? 'Something went wrong';
		} finally {
			submitting = false;
		}
	}
</script>

<!-- Brand: "flows" where the l is a 180°-rotated 7 (per Figma). -->
<div class="absolute left-6 top-4 flex items-baseline text-[26px] tracking-tight select-none">
	<span>f</span>
	<span class="inline-block rotate-180">7</span>
	<span>ows</span>
</div>

<div class="flex h-screen items-center justify-center">
	<form
		class="flex w-[300px] flex-col gap-3"
		onsubmit={(e) => {
			e.preventDefault();
			submit();
		}}
	>
		<h1 class="mb-2 text-[26px]">{mode === 'login' ? 'Login' : 'Signup'}</h1>

		<label class="flex flex-col gap-1">
			<span class="text-[15px]">email</span>
			<input
				type="email"
				bind:value={email}
				required
				autocomplete="email"
				class="border border-paper bg-ink px-3 py-2 text-[15px] focus:outline-none"
			/>
		</label>

		<label class="flex flex-col gap-1">
			<span class="text-[15px]">password</span>
			<input
				type="password"
				bind:value={password}
				required
				autocomplete={mode === 'login' ? 'current-password' : 'new-password'}
				class="border border-paper bg-ink px-3 py-2 text-[15px] focus:outline-none"
			/>
		</label>

		{#if error}<p class="text-sm text-red-400">{error}</p>{/if}

		<div class="flex items-center justify-between">
			<button
				type="button"
				class="text-sm text-paper/60 hover:text-accent"
				onclick={() => (mode = mode === 'login' ? 'signup' : 'login')}
			>
				{mode === 'login' ? 'Need an account? Sign up' : 'Have an account? Log in'}
			</button>
			<Button type="submit" disabled={submitting}>{submitting ? '…' : 'Go'}</Button>
		</div>
	</form>
</div>
