/** Normalize line-start display formulas without touching fenced code. */
export function normalizeDisplayMath(content: string): string {
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
