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

it("renders the selection translation's LaTeX variables and numbered fractions", () => {
  const html = render(String.raw`定义 1。设 \(S_A\) 从发言者到节点 \(v_i\) 的延迟区间为 \([d^A_{v_i},D^A_{v_i}]\)。

\[
\alpha_{v_i}(S_A,S_B)=\frac{1}{1+\left|\frac{D^A_{v_i}+d^A_{v_i}}{2}-\frac{D^B_{v_i}+d^B_{v_i}}{2}\right|}.\tag{1}
\]

类似地，区间长度的接近度为：

\[\beta_{v_i}(S_A,S_B)=\frac{1}{1+|(D^A_{v_i}-d^A_{v_i})-(D^B_{v_i}-d^B_{v_i})|}.\tag{2}\]

其值介于 0 和 1 之间。`);
  assert.equal((html.match(/class="katex-display"/g) || []).length, 2);
  assert.equal((html.match(/class="katex"/g) || []).length, 5);
  assert.match(html, /<mfrac>/);
  assert.match(html, /<msubsup>/);
  assert.match(html, /class="tag"/);
  assert.doesNotMatch(html, /math-fallback|katex-error/);
  assert.match(html, /<p>其值介于 0 和 1 之间。<\/p>/);
});

it("leaves code, links, existing math, escaped and incomplete delimiters untouched", () => {
  for (const text of [
    "代码 `\\(x_i\\)`",
    "```tex\n\\[x_i\\]\n```", "    \\(x_i\\)",
    "> ```tex\n> \\[x_i\\]\n> ```", "- `\\(x_i\\)`",
    "[link](https://example.com/\\(x\\))", String.raw`$\text{\(literal\)}$`,
    String.raw`\\(literal\\)`, String.raw`缺少结尾 \(x_i`, String.raw`\[x_i`,
  ]) assert.equal(normalizeDisplayMath(text), text);
});

it("keeps display math in its paragraph, blockquote or list container", () => {
  for (const text of [String.raw`前文 \[x=1\] 后文`, "> \\[x=1\\]", "- \\[x=1\\]", "> \\[\n> x=1\n> \\]", "- \\[\n  x=1\n  \\]"]) {
    const html = render(text);
    assert.match(html, /katex-display/);
    assert.doesNotMatch(html, /math-fallback|katex-error/);
    assert.match(html, /<annotation encoding="application\/x-tex">x=1<\/annotation>/);
    assert.equal(normalizeDisplayMath(normalizeDisplayMath(text)), normalizeDisplayMath(text));
    if (text.startsWith(">")) assert.match(html, /<blockquote>\s*<span class="katex-display"/);
    if (text.startsWith("-")) assert.match(html, /<li>\s*<span class="katex-display"/);
  }
});
