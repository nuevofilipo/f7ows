/** Settings endpoints (`/settings`). Effective prefs + schema + raw overrides. */
import { api } from './client.js';

/**
 * @typedef {Object} SettingsPayload
 * @property {Record<string, any>} effective  resolved values
 * @property {Record<string, any>} overrides  raw stored overrides
 * @property {{ key: string, type: string, default: any, min: number|null, max: number|null, choices: string[]|null, description: string }[]} schema
 */

/** @returns {Promise<SettingsPayload>} */
export const getSettings = () => api.get('/settings');

/** @param {Record<string, unknown>} patch @returns {Promise<SettingsPayload>} */
export const updateSettings = (patch) => api.put('/settings', patch);
