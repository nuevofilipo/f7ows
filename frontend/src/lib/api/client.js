/**
 * Tiny fetch wrapper around the backend HTTP API.
 *
 * - Always sends the auth cookie (`credentials: 'include'`) — the backend issues
 *   an HTTP-only JWT cookie, so there is no token to manage in JS.
 * - JSON in, JSON out; throws {@link ApiError} (carrying status + detail) on any
 *   non-2xx so callers can branch on `err.status` (e.g. 401 -> log out).
 */
import { API_BASE } from '$lib/config.js';

export class ApiError extends Error {
	/** @param {number} status @param {string} detail */
	constructor(status, detail) {
		super(detail || `Request failed (${status})`);
		this.name = 'ApiError';
		this.status = status;
		this.detail = detail;
	}
}

/**
 * @param {string} path  e.g. "/auth/me"
 * @param {{ method?: string, body?: unknown, raw?: BodyInit, headers?: Record<string,string> }} [opts]
 */
export async function request(path, opts = {}) {
	const { method = 'GET', body, raw, headers: extraHeaders = {} } = opts;

	/** @type {Record<string, string>} */
	const headers = { ...extraHeaders };
	/** @type {RequestInit} */
	const init = { method, credentials: 'include', headers };

	if (raw !== undefined) {
		init.body = raw; // FormData etc. — let the browser set the content-type.
	} else if (body !== undefined) {
		headers['Content-Type'] = 'application/json';
		init.body = JSON.stringify(body);
	}

	const res = await fetch(`${API_BASE}${path}`, init);

	if (res.status === 204) return null;

	const text = await res.text();
	const data = text ? safeJson(text) : null;

	if (!res.ok) {
		const detail = (data && (data.detail ?? data.message)) || res.statusText;
		throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
	}
	return data;
}

/** @param {string} text */
function safeJson(text) {
	try {
		return JSON.parse(text);
	} catch {
		return text;
	}
}

export const api = {
	get: (/** @type {string} */ p) => request(p),
	post: (/** @type {string} */ p, /** @type {unknown} */ body = undefined) =>
		request(p, body === undefined ? { method: 'POST' } : { method: 'POST', body }),
	put: (/** @type {string} */ p, /** @type {unknown} */ body) => request(p, { method: 'PUT', body }),
	patch: (/** @type {string} */ p, /** @type {unknown} */ body) => request(p, { method: 'PATCH', body }),
	delete: (/** @type {string} */ p) => request(p, { method: 'DELETE' }),
	/** @param {string} p @param {BodyInit} raw */
	postRaw: (p, raw) => request(p, { method: 'POST', raw })
};
