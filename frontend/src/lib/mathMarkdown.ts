import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkMath from "remark-math";

const mathParser = unified().use(remarkParse).use(remarkMath);

/** Recognize model-produced LaTeX delimiters before Markdown consumes the escapes.
 * Markdown positions protect code, links and existing dollar math verbatim. */
function normalizeLatexDelimiters(content: string): string {
  if (!/\\[([]/.test(content)) return content;
  type Node = { type: string; children?: Node[]; position?: {
    start: { offset?: number }; end: { offset?: number };
  } };
  const protectedRanges: [number, number][] = [];
  function visit(node: Node) {
    if (["code", "inlineCode", "html", "link", "linkReference", "image", "imageReference", "definition", "math", "inlineMath"].includes(node.type)) {
      const start = node.position?.start.offset, end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) protectedRanges.push([start, end]);
    } else node.children?.forEach(visit);
  }
  visit(mathParser.parse(content));
  const edits: { start: number; end: number; value: string }[] = [];
  function convert(start: number, end: number) {
    const tokens = content.slice(start, end).matchAll(/\\+[()[\]]/g);
    let opener: { start: number; end: number; display: boolean } | undefined;
    for (const token of tokens) {
      if ((token[0].length - 1) % 2 === 0) continue; // Escaped backslash.
      const symbol = token[0].slice(-1);
      const at = start + token.index + token[0].length - 2;
      if (symbol === "(" || symbol === "[") {
        opener = { start: at, end: at + 2, display: symbol === "[" };
      } else if (opener && symbol === (opener.display ? "]" : ")")) {
        const body = content.slice(opener.end, at).trim();
        if (body && (opener.display || !/\n\s*\n/.test(body))) {
          let value = `$${body}$`;
          if (opener.display) {
            const before = content.slice(content.lastIndexOf("\n", opener.start - 1) + 1, opener.start);
            // Retain blockquote/list indentation when creating a display block.
            const container = before.match(/^([ \t]*(?:>[ \t]*)*)(?:(?:[-+*]|\d+[.)])([ \t]+))?/)!;
            const prefix = container[1] + " ".repeat(container[0].length - container[1].length);
            const lines = content.slice(opener.end, at).split("\n")
              .map((line, i) => i && line.startsWith(prefix) ? line.slice(prefix.length) : line)
              .join("\n").trim().split("\n");
            const lead = before === container[0] ? "" : `\n${prefix}`;
            const after = content.slice(at + 2, content.indexOf("\n", at + 2) < 0 ? end : content.indexOf("\n", at + 2));
            value = `${lead}$$\n${prefix}${lines.join(`\n${prefix}`)}\n${prefix}$$${after.trim() ? `\n${prefix}` : ""}`;
          }
          edits.push({ start: opener.start, end: at + 2, value });
        }
        opener = undefined;
      }
    }
  }
  let start = 0;
  for (const [from, to] of protectedRanges) {
    convert(start, from);
    start = to;
  }
  convert(start, content.length);
  for (const edit of edits.reverse()) content = content.slice(0, edit.start) + edit.value + content.slice(edit.end);
  return content;
}

/** Normalize line-start display formulas without touching fenced code. */
export function normalizeDisplayMath(content: string): string {
  content = normalizeLatexDelimiters(content);
  const lines = content.split(/\r?\n/);
  const output: string[] = [];
  let fence: string | undefined;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && /^ {0,3}(?:`+|~+)\s*$/.test(line)) fence = undefined;
      output.push(line);
      continue;
    }
    if (fence || !/^ {0,3}\$\$(?!\$)/.test(line)) {
      output.push(line);
      continue;
    }
    const body = [line.replace(/^ {0,3}\$\$/, "")];
    let end = i;
    while (!/(?<!\\)\$\$\s*$/.test(body[body.length - 1]) && end + 1 < lines.length) {
      // A paragraph boundary means we cannot safely infer the missing closer.
      if (!lines[end + 1].trim()) break;
      body.push(lines[++end]);
    }
    if (/(?<!\\)\$\$\s*$/.test(body[body.length - 1])) {
      body[body.length - 1] = body[body.length - 1].replace(/\$\$\s*$/, "");
      if (body[0] === "") body.shift();
      if (body[body.length - 1] === "") body.pop();
      output.push("$$", ...body, "$$");
      i = end;
    } else output.push(line);
  }
  return output.join("\n");
}

/** GFM splits table cells before remark-math parses them. Shield pipes only in
 * complete math spans, then restore their exact TeX before KaTeX sees it.
 * Escaping a pipe as \| is not equivalent: TeX renders that as a double bar. */
export function prepareMathMarkdown(source: string) {
  let content = normalizeDisplayMath(source);
  let token = "\uE000MATH_PIPE\uE001";
  while (content.includes(token)) token += "_";
  const ranges: [number, number][] = [];
  type MarkdownNode = { type: string; children?: MarkdownNode[]; position?: {
    start: { offset?: number }; end: { offset?: number };
  } };
  function findMath(node: MarkdownNode) {
    if (node.type === "math" || node.type === "inlineMath") {
      const start = node.position?.start.offset, end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) {
        const span = content.slice(start, end);
        // A GFM cell cannot span lines. Do not pair currency in separate cells
        // (e.g. "$5 | $10") and accidentally hide a real column separator.
        if (node.type === "inlineMath" && (/\n/.test(span) || (/^\$\d/.test(span) && /\d/.test(content[end] || "")))) return;
        ranges.push([start, end]);
      }
    } else node.children?.forEach(findMath);
  }
  if (content.includes("|")) findMath(mathParser.parse(content));
  for (const [start, end] of ranges.reverse()) {
    content = content.slice(0, start) + content.slice(start, end).replaceAll("|", token) + content.slice(end);
  }
  function restoreMathPipes() {
    type HtmlNode = { type: string; value?: string; properties?: Record<string, unknown>; children?: HtmlNode[] };
    return (tree: HtmlNode) => {
      function visit(node: HtmlNode, math = false) {
        const classes = node.properties?.className;
        const inMath = math || (Array.isArray(classes) && classes.includes("language-math"));
        if (inMath && node.type === "text" && node.value) node.value = node.value.replaceAll(token, "|");
        node.children?.forEach(child => visit(child, inMath));
      }
      visit(tree);
    };
  }
  return { content, restoreMathPipes };
}

/** Preserve failed expressions as readable text instead of a red error wall. */
export function readableMathErrors() {
  type Node = { type: string; properties?: Record<string, unknown>; children?: Node[] };
  return (tree: Node) => {
    function visit(node: Node) {
      const classes = node.properties?.className;
      if (Array.isArray(classes) && classes.includes("katex-error")) {
        node.properties = { className: ["math-fallback"], title: "公式未能解析，显示原文" };
      }
      node.children?.forEach(visit);
    }
    visit(tree);
  };
}
