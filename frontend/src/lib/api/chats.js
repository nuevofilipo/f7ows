/** Chat endpoints (`/projects/{id}/chats`). */
import { api } from './client.js';

/**
 * @typedef {Object} Message
 * @property {string} id
 * @property {string} chat_id
 * @property {string} path        ltree path — gives reddit-style nesting
 * @property {'user'|'assistant'} role
 * @property {string} content
 * @property {Record<string, any>} metadata
 * @property {string} created_at
 */

/** @typedef {{ chat_id: string, message_count: number, last_activity: string }} ChatSummary */

/** @param {string} projectId @returns {Promise<ChatSummary[]>} */
export const listChats = (projectId) => api.get(`/projects/${projectId}/chats`);

/** @param {string} projectId @param {string} chatId @returns {Promise<Message[]>} */
export const listMessages = (projectId, chatId) =>
	api.get(`/projects/${projectId}/chats/${chatId}/messages`);

/**
 * Post a message and get the grounded reply. There is no "create chat" endpoint:
 * a chat exists once you POST to a fresh client-generated UUID.
 *
 * @param {string} projectId
 * @param {string} chatId
 * @param {Object} payload
 * @param {string} payload.content
 * @param {string} [payload.parent_path]  branch under a specific message
 * @param {string} [payload.command]      e.g. "mockexam" | "deepsummary"
 * @param {string} [payload.intent]
 * @param {number} [payload.level]         visual hierarchy level (1–3) for the new turn
 * @param {Record<string, unknown>} [payload.settings]  per-request preference overrides
 * @returns {Promise<{ user_message: Message, assistant_message: Message, cited?: any[], retrieved?: any[], [k:string]: any }>}
 */
export const sendMessage = (projectId, chatId, payload) =>
	api.post(`/projects/${projectId}/chats/${chatId}/messages`, payload);

/** @returns {Promise<{ name: string, description: string, instructions: string, intent: string|null, background: boolean }[]>} */
export const listCommands = () => api.get('/commands');
