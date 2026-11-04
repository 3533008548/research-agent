import assert from "node:assert/strict";
import { it } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { normalizeDisplayMath, readableMathErrors } from "./mathMarkdown.ts";

function render(content: string) {
  return renderToStaticMarkup(createElement(ReactMarkdown, {
    remarkPlugins: [remarkMath], rehypePlugins: [rehypeKatex, readableMathErrors],
    children: normalizeDisplayMath(content),
  }));
}

it("keeps paragraphs and headings after a multiline formula outside math", () => {
  const html = render("$$\\alpha=1,\\qquad\n\\beta=2\\tag{1,2}$$\n\n## 后续标题\n\n**正文** $x$\n\n$$y=3$$");
  assert.match(html, /<h2>后续标题<\/h2>/);
  assert.match(html, /<strong>正文<\/strong>/);
  assert.doesNotMatch(html, /math-fallback|katex-error/);
  assert.match(html, /katex-display/);
});

it("preserves code, inline math and already normalized blocks", () => {
  for (const text of ["```tex\n$$x$$\n```", "~~~\n$$x$$\n~~~", "正文 $x$ 与 `$$y$$`", "$$\nx\n$$"]) {
    assert.equal(normalizeDisplayMath(text), text);
  }
});

it("shows invalid TeX as escaped readable source", () => {
  const html = render("$$\\frac{<script>$$\n\n## 正文");
  assert.match(html, /math-fallback/);
  assert.doesNotMatch(html, /katex-error|<script>/);
  assert.match(html, /<h2>正文<\/h2>/);
});
