/** Auth endpoints (`/auth`). Cookie is set/cleared by the server. */
import { api } from './client.js';

/** @typedef {{ id: string, email: string, settings: Record<string, unknown>, created_at: string }} User */

/** @param {string} email @param {string} password @returns {Promise<User>} */
export const register = (email, password) => api.post('/auth/register', { email, password });

/** @param {string} email @param {string} password @returns {Promise<User>} */
export const login = (email, password) => api.post('/auth/login', { email, password });

export const logout = () => api.post('/auth/logout');

/** @returns {Promise<User>} */
export const me = () => api.get('/auth/me');
