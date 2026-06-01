/**
 * Render assistant markdown (with KaTeX math) to sanitized HTML.
 *
 * Answers come back as markdown containing `$...$` / `$$...$$` math and `[S#]`
 * citation markers. We render markdown + KaTeX, then sanitize — KaTeX output is
 * trusted but the surrounding model text is not, so DOMPurify is the safety net.
 */
import { marked } from 'marked';
import markedKatex from 'marked-katex-extension';
import DOMPurify from 'dompurify';

marked.use(
	markedKatex({ throwOnError: false, nonStandard: true }),
	{ breaks: true, gfm: true }
);

/** @param {string} src @returns {string} sanitized HTML */
export function renderMarkdown(src) {
	const pre = normalizeDisplayMath(convertTexDelimiters(src ?? ''));
	const html = marked.parse(pre, { async: false });
	return DOMPurify.sanitize(/** @type {string} */ (html));
}

/**
 * Models often emit math with LaTeX delimiters `\( … \)` (inline) and `\[ … \]`
 * (display) instead of `$ … $` / `$$ … $$`. marked-katex only understands the
 * dollar forms, so without this the backslash-parens are just unescaped to plain
 * `( … )` and the math never renders. Convert them up front.
 * @param {string} src
 */
function convertTexDelimiters(src) {
	return src
		.replace(/\\\[([\s\S]*?)\\\]/g, (_, body) => `$$${body}$$`)
		.replace(/\\\(([\s\S]*?)\\\)/g, (_, body) => `$${body}$`);
}

/**
 * Ensure a `$$` display-math fence is preceded by a blank line. Without it,
 * marked folds the `$$` into the previous paragraph and the block-math
 * tokenizer never fires, so the math renders as raw `$$…$$` text. We only pad
 * *opening* fences (tracked via a toggle) so we never split a math block.
 * @param {string} src
 */
function normalizeDisplayMath(src) {
	const lines = src.split('\n');
	/** @type {string[]} */
	const out = [];
	let inMath = false;
	for (const line of lines) {
		const isFence = line.trim() === '$$';
		if (isFence && !inMath) {
			if (out.length && out[out.length - 1].trim() !== '') out.push('');
			out.push(line);
			inMath = true;
		} else {
			out.push(line);
			if (isFence && inMath) inMath = false;
		}
	}
	return out.join('\n');
}
