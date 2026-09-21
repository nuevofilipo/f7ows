/**
 * Session state — the current user, loaded from `GET /auth/me`.
 *
 * Auth is a cross-origin HTTP-only cookie, so the SvelteKit server can't see it;
 * all auth lives in the browser. This rune store is the single source of truth
 * for "am I logged in", initialised once from the root layout.
 */
import * as auth from '$lib/api/auth.js';
import { ApiError } from '$lib/api/client.js';

class SessionStore {
	/** @type {import('$lib/api/auth.js').User | null} */
	user = $state(null);
	/** true until the first /me check resolves, so guards can wait. */
	loading = $state(true);

	get authenticated() {
		return this.user !== null;
	}

	/**
	 * True when the backend runs in single-user (desktop) mode: it seeds one local
	 * user and auto-issues the cookie, so there is nobody to log in as and nothing
	 * to log out to. The UI hides the account controls rather than offering a
	 * button whose only effect is to be undone on the next request.
	 */
	get singleUser() {
		return this.user?.single_user === true;
	}

	/** Load the current user once on boot; swallow 401 (just means logged out). */
	async init() {
		try {
			this.user = await auth.me();
		} catch (err) {
			if (!(err instanceof ApiError) || err.status !== 401) console.error(err);
			this.user = null;
		} finally {
			this.loading = false;
		}
	}

	/** @param {string} email @param {string} password */
	async login(email, password) {
		this.user = await auth.login(email, password);
	}

	/** @param {string} email @param {string} password */
	async register(email, password) {
		this.user = await auth.register(email, password);
	}

	async logout() {
		try {
			await auth.logout();
		} finally {
			this.user = null;
		}
	}
}

export const session = new SessionStore();
