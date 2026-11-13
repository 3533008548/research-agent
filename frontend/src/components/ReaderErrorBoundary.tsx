import { Component, type ReactNode, type ErrorInfo } from "react";

export class ReaderErrorBoundary extends Component<{ children: ReactNode; title?: string }, { error: string }> {
  state = { error: "" };
  static getDerivedStateFromError(error: Error) {
    return { error: error.message || "阅读器加载失败" };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Paper reader failed", error, info.componentStack);
    window.desktop?.reportError?.(`${error.name}: ${error.message}\n${error.stack || ""}\n${info.componentStack || ""}`);
  }
  render() {
    if (this.state.error) return <section className="reader-load-error" role="alert">
      <h2>{this.props.title || "论文预览未能打开"}</h2>
      <p>{!this.props.title && "可以继续使用其他标签页。"}请重新加载界面后重试，已保存的论文和笔记会保留。</p>
      <button className="button button-primary" onClick={() => window.location.reload()}>重新加载界面</button>
      <details><summary>错误详情</summary><pre>{this.state.error}</pre></details>
    </section>;
    return this.props.children;
  }
}
