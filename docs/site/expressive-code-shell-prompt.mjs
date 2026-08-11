// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/**
 * Expressive Code plugin that prefixes shell command lines with a decorative
 * `$` prompt.
 *
 * The prompt is rendered into Expressive Code's gutter column rather than into
 * the code text, so it never reaches the clipboard: the copy control copies the
 * block's plain-text source, and the engine marks every gutter element
 * `pointer-events: none; user-select: none`. Keeping the prompt out of the
 * source also keeps the Bash grammar tokenizing the commands themselves, which
 * a literal `$ ` prefix in the Markdown would not.
 *
 * A prompt marks the start of a command. Blank lines, comment lines, and lines
 * continuing a preceding backslash-terminated command render an empty gutter
 * element of the same width so the code column stays aligned.
 */

const shellLanguages = new Set(['bash', 'sh', 'shell', 'zsh', 'console', 'shellsession']);
const prompt = '$';

function isBlank(text) {
  return text.trim().length === 0;
}

function isComment(text) {
  return text.trimStart().startsWith('#');
}

function continuesPreviousCommand(lines, index) {
  if (index === 0) return false;
  const previous = lines[index - 1].text;
  return !isComment(previous) && previous.trimEnd().endsWith('\\');
}

function showsPrompt(lines, index) {
  const { text } = lines[index];
  return !isBlank(text) && !isComment(text) && !continuesPreviousCommand(lines, index);
}

function promptElement(visible) {
  return {
    type: 'element',
    tagName: 'span',
    properties: { className: ['shell-prompt'], 'aria-hidden': 'true' },
    children: visible ? [{ type: 'text', value: prompt }] : [],
  };
}

const baseStyles = `
	.frame.is-terminal .gutter .shell-prompt {
		display: inline-block;
		box-sizing: content-box;
		width: 1ch;
		padding-inline-start: var(--ec-codePadInl);
		padding-inline-end: 1ch;
		font-weight: 600;
	}

	.frame.is-terminal .${'ec-line'} .gutter ~ .code {
		padding-inline-start: calc(var(--ecIndent, 0ch) - var(--ecGtrBrdWd));
	}
`;

/**
 * @returns {{ name: string, baseStyles: string, hooks: object }} plugin definition
 */
export function shellPrompt() {
  return {
    name: 'Picogrid shell prompt',
    baseStyles,
    hooks: {
      annotateCode: ({ codeBlock, addGutterElement }) => {
        if (!shellLanguages.has(codeBlock.language)) return;
        const lines = codeBlock.getLines();
        addGutterElement({
          renderLine: ({ lineIndex }) => promptElement(showsPrompt(lines, lineIndex)),
          renderPlaceholder: () => promptElement(false),
        });
      },
    },
  };
}
