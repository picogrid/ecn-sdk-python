// Copyright (c) Picogrid, Inc.
// SPDX-License-Identifier: MPL-2.0

/** Shared JSONC reader: the deploy gate and the deploy workflow read one config. */

/**
 * Strip JSONC comments and trailing commas with a string-aware scan.
 *
 * Regex stripping cannot tell a comment from comment-like text inside a JSON
 * string, and it misses end-of-line comments after a value — both of which
 * appear in a normal `wrangler.jsonc`.
 */
export function parseJsonc(source) {
  let out = '';
  let inString = false;
  let escaped = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];

    if (inString) {
      out += character;
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') inString = false;
      continue;
    }

    if (character === '"') {
      inString = true;
      out += character;
      continue;
    }

    if (character === '/' && source[index + 1] === '/') {
      const end = source.indexOf('\n', index);
      index = end === -1 ? source.length : end - 1;
      continue;
    }

    if (character === '/' && source[index + 1] === '*') {
      const end = source.indexOf('*/', index + 2);
      if (end === -1) {
        throw new SyntaxError(`unterminated block comment at offset ${index}`);
      }
      index = end + 1;
      continue;
    }

    // Trailing commas are legal JSONC and rejected by JSON.parse. Dropping them
    // here — rather than by regex over the whole document — keeps commas that
    // live inside strings untouched.
    if (character === '}' || character === ']') {
      const trimmed = out.replace(/\s+$/, '');
      if (trimmed.endsWith(',')) out = trimmed.slice(0, -1);
    }

    out += character;
  }

  if (inString) {
    throw new SyntaxError('unterminated string literal in JSONC input');
  }

  return JSON.parse(out);
}
