import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "katex/dist/katex.min.css";
import "./styles.css";
import "./workbench.css";
import { App } from "./App";
import { ReaderErrorBoundary } from "./components/ReaderErrorBoundary";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ReaderErrorBoundary title="界面暂时无法显示"><App /></ReaderErrorBoundary>
  </StrictMode>,
);
