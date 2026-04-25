import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown', () => {
  it('wraps plain paragraphs', () => {
    expect(renderMarkdown('hello')).toContain('<p>hello</p>');
  });

  it('highlights TS code blocks', () => {
    const html = renderMarkdown('```ts\nconst x = 1;\n```');
    expect(html).toContain('class="hljs language-ts"');
    // hljs emits spans with class="hljs-keyword" etc.
    expect(html).toContain('hljs-');
  });

  it('falls back to plaintext for unknown lang', () => {
    const html = renderMarkdown('```nope\nfoo\n```');
    expect(html).toContain('class="hljs language-nope"');
    // No throw.
  });

  it('strips <script> tags', () => {
    const html = renderMarkdown(`<script>alert('x')</script>hello`);
    expect(html).not.toContain('<script');
    expect(html).toContain('hello');
  });

  it('strips javascript: hrefs', () => {
    const html = renderMarkdown(`[link](javascript:alert('x'))`);
    expect(html).not.toMatch(/href="javascript:/i);
  });

  it('adds target=_blank rel=noopener to external links', () => {
    const html = renderMarkdown(`[ex](https://example.com)`);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener"');
  });

  it('preserves bold and inline code', () => {
    const html = renderMarkdown('**bold** and `code`');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<code>code</code>');
  });
});
