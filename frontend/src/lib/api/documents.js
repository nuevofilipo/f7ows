/** Documents endpoints (`/projects/{id}/documents`). PDF sources for a project. */
import { api, request } from './client.js';

/**
 * @typedef {Object} Document
 * @property {string} id
 * @property {string} name
 * @property {string} storage_key
 * @property {string} content_type
 * @property {number} size_bytes
 * @property {{ state?: string, [k: string]: unknown }} status
 * @property {string} created_at
 */

/** @param {string} projectId @returns {Promise<Document[]>} */
export const listDocuments = (projectId) => api.get(`/projects/${projectId}/documents`);

/**
 * Upload one or many PDFs. The backend has a single and a /batch route; we always
 * use /batch and return { uploaded, errors } so multi-file selection just works.
 * @param {string} projectId
 * @param {File[]} files
 * @returns {Promise<{ uploaded: Document[], errors: { filename: string, error: string }[] }>}
 */
export function uploadDocuments(projectId, files) {
	const form = new FormData();
	for (const f of files) form.append('files', f);
	return request(`/projects/${projectId}/documents/batch`, { method: 'POST', raw: form });
}
