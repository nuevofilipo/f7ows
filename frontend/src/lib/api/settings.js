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

// ── Model providers (per role) ────────────────────────────────────────────────
// Each of chat / planner / embed has its own endpoint, model and credential ref,
// so the cheap planner can run on a local Ollama while chat stays on a frontier
// model. `key_ref` names a key in the OS keychain; an empty one means the
// endpoint needs no auth (the normal case for Ollama).

/**
 * @typedef {Object} Provider
 * @property {string} role       "chat" | "planner" | "embed"
 * @property {string} base_url   OpenAI-compatible base, e.g. http://localhost:11434/v1
 * @property {string} model
 * @property {string} key_ref    keychain entry name; "" = no auth needed
 * @property {boolean} keyless
 * @property {boolean} key_available  whether the referenced key actually resolves
 */

/** @returns {Promise<{ providers: Provider[], roles: string[] }>} */
export const getProviders = () => api.get('/settings/providers');

/** Update one role. Only the fields passed are written.
 * @param {string} role
 * @param {{ base_url?: string, model?: string, key_ref?: string }} patch
 * @returns {Promise<{ providers: Provider[], roles: string[] }>} */
export const updateProvider = (role, patch) => api.put(`/settings/providers/${role}`, patch);

/** Store a credential under a named ref (for a provider that isn't OpenRouter).
 * @param {string} ref @param {string} key */
export const setProviderKey = (ref, key) => api.put(`/settings/keys/${ref}`, { key });

/** Remove the credential stored under `ref`. @param {string} ref */
export const clearProviderKey = (ref) => request(`/settings/keys/${ref}`, { method: 'DELETE' });
