/** Projects endpoints (`/projects`). A project is one notebook. */
import { api } from './client.js';

/** @typedef {{ id: string, name: string, description: string|null, created_at: string, updated_at: string }} Project */

/** @returns {Promise<Project[]>} */
export const listProjects = () => api.get('/projects');

/** @param {string} name @param {string} [description] @returns {Promise<Project>} */
export const createProject = (name, description) =>
	api.post('/projects', { name, description });

/** Rename / edit a project. @param {string} id @param {{ name?: string, description?: string }} patch @returns {Promise<Project>} */
export const updateProject = (id, patch) => api.patch(`/projects/${id}`, patch);

/** Delete a project and everything under it (sources, chats, outputs). @param {string} id */
export const deleteProject = (id) => api.delete(`/projects/${id}`);
