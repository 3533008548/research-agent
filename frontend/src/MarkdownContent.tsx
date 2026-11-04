import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { normalizeDisplayMath, readableMathErrors } from "./lib/mathMarkdown";

export default function MarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex, readableMathErrors]}>
      {normalizeDisplayMath(content)}
    </ReactMarkdown>
  );
}
