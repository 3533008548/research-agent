import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import { prepareMathMarkdown, readableMathErrors } from "./lib/mathMarkdown";

export default function MarkdownContent({ content }: { content: string }) {
  const math = prepareMathMarkdown(content);
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[math.restoreMathPipes, rehypeKatex, readableMathErrors]}>
      {math.content}
    </ReactMarkdown>
  );
}
