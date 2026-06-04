/** Projection endpoints (`/projects/{id}/projection`). Data for the map page. */
import { api } from './client.js';

/**
 * @typedef {{ x: number, y: number }} Coord
 * @typedef {Object} ProjectionChunk
 * @property {string} id
 * @property {string} document_name
 * @property {string|null} path
 * @property {string[]} heading_trail
 * @property {number} token_count
 * @property {'actively_studied'|'passively_studied'|'not_studied'} study
 * @property {{ umap_2d: Coord|null, pca: Coord|null, tsne: Coord|null }} coords
 */

/**
 * Every chunk in the project with its 2D display coordinates + study state.
 * `ready` is false until the worker has projected the project.
 * @param {string} projectId
 * @returns {Promise<{ ready: boolean, chunks: ProjectionChunk[] }>}
 */
export const getProjection = (projectId) => api.get(`/projects/${projectId}/projection`);

/**
 * Rank chunks against a query; the map highlights the top-`k` returned ids.
 * @param {string} projectId
 * @param {string} q
 * @param {'cosine'|'bm25'} method
 * @param {number} k  how many top matches to highlight
 * @returns {Promise<{ method: string, query: string, matches: { id: string, score: number }[] }>}
 */
export const searchProjection = (projectId, q, method, k) =>
	api.get(`/projects/${projectId}/projection/search?q=${encodeURIComponent(q)}&method=${method}&k=${k}`);

/**
 * The full Markdown text of one chunk (lazy-loaded when a point is clicked).
 * @param {string} projectId
 * @param {string} chunkId
 * @returns {Promise<{ id: string, content_full: string }>}
 */
export const getProjectionChunk = (projectId, chunkId) =>
	api.get(`/projects/${projectId}/projection/chunks/${chunkId}`);
