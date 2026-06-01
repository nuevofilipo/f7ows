/** Projects endpoints (`/projects`). A project is one notebook. */
import { api } from './client.js';

/** @typedef {{ id: string, name: string, description: string|null, created_at: string, updated_at: string }} Project */

/** @returns {Promise<Project[]>} */
export const listProjects = () => api.get('/projects');

/** @param {string} name @param {string} [description] @returns {Promise<Project>} */
export const createProject = (name, description) =>
	api.post('/projects', { name, description });
