import { marked, type Tokens } from 'marked';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/core';

// ---- Language registrations (curated set; no Shiki/WASM) ----

import typescript from 'highlight.js/lib/languages/typescript';
import javascript from 'highlight.js/lib/languages/javascript';
import python from 'highlight.js/lib/languages/python';
import bash from 'highlight.js/lib/languages/bash';
import json from 'highlight.js/lib/languages/json';
import xml from 'highlight.js/lib/languages/xml';
import css from 'highlight.js/lib/languages/css';
import sql from 'highlight.js/lib/languages/sql';
import go from 'highlight.js/lib/languages/go';
import rust from 'highlight.js/lib/languages/rust';
import markdown from 'highlight.js/lib/languages/markdown';
import plaintext from 'highlight.js/lib/languages/plaintext';

// Canonical names
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('python', python);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('json', json);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('css', css);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('go', go);
hljs.registerLanguage('rust', rust);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('plaintext', plaintext);

// Aliases
hljs.registerLanguage('ts', typescript);
hljs.registerLanguage('js', javascript);
hljs.registerLanguage('py', python);
hljs.registerLanguage('sh', bash);
hljs.registerLanguage('html', xml);   // HTML blocks parsed via xml grammar
hljs.registerLanguage('md', markdown);

// ---- Marked custom renderer ----

marked.use({
  renderer: {
    // Block-level code: run highlight.js inline, emit pre/code with original lang class.
    code({ text, lang }: Tokens.Code): string {
      // Keep the original lang token for the class attribute (even if unregistered).
      const displayLang = lang ?? '';
      // Resolve to a registered grammar; fall back to plaintext.
      const resolvedLang = (lang && hljs.getLanguage(lang)) ? lang : 'plaintext';
      const highlighted = hljs.highlight(text, {
        language: resolvedLang,
        ignoreIllegals: true,
      }).value;
      const cls = displayLang
        ? `hljs language-${displayLang}`
        : 'hljs language-plaintext';
      return `<pre><code class="${cls}">${highlighted}</code></pre>\n`;
    },

    // Inline links: add target/_blank for external safety.
    link({ href, title, tokens }: Tokens.Link): string {
      const text = this.parser.parseInline(tokens);
      let out = `<a href="${href}"`;
      if (title) {
        out += ` title="${title}"`;
      }
      out += ` target="_blank" rel="noopener">${text}</a>`;
      return out;
    },
  },
});

// ---- Public API ----

/**
 * Render markdown to sanitized HTML.
 *
 * The pipeline runs once — no separate post-pass:
 *   1. marked.parse (with custom code + link renderers) → HTML with syntax-colored code blocks
 *   2. DOMPurify.sanitize → strips <script>, on*, javascript: hrefs; preserves class/target/rel
 */
export function renderMarkdown(text: string): string {
  // marked v14 sync parse returns string directly.
  const html = marked.parse(text) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['class', 'target', 'rel'],
  });
}
