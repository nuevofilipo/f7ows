/** Settings endpoints (`/settings`). Effective prefs + schema + raw overrides. */
import { api, request } from './client.js';

/**
 * @typedef {Object} SettingsPayload
 * @property {Record<string, any>} effective  resolved values
 * @property {Record<string, any>} overrides  raw stored overrides
 * @property {{ key: string, type: string, default: any, min: number|null, max: number|null, choices: string[]|null, group: string, description: string }[]} schema
 * @property {boolean} openrouter_key_set  whether the user has stored a personal key
 * @property {string|null} openrouter_key_preview  masked preview (e.g. "sk-or…3f9a"), never the full key
 */

/** @typedef {{ openrouter_key_set: boolean, openrouter_key_preview: string|null }} KeyStatus */

/** @returns {Promise<SettingsPayload>} */
export const getSettings = () => api.get('/settings');

/** @typedef {{ id: string, name: string }} ModelOption */

/** The gateway's model catalogue (fetched live — call only when the picker opens).
 * @returns {Promise<{ models: ModelOption[] }>} */
export const getModels = () => api.get('/settings/models');

/** @param {Record<string, unknown>} patch @returns {Promise<SettingsPayload>} */
export const updateSettings = (patch) => api.put('/settings', patch);

/** Store/replace the user's OpenRouter key (sent once, never read back).
 * @param {string} key @returns {Promise<KeyStatus>} */
export const setOpenRouterKey = (key) => api.put('/settings/openrouter-key', { key });

/** Remove the stored key — traffic falls back to the server's global key.
 * @returns {Promise<KeyStatus>} */
export const clearOpenRouterKey = () => request('/settings/openrouter-key', { method: 'DELETE' });
