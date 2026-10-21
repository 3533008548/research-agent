import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  appendRunEvent,
  badcaseCategoryLabel,
  codeSourceLabel,
  dailyPaperStatusLabel,
  dailyRunTitle,
  displayMessages,
  durationLabel,
  experimentCodeSource,
  ledgerKindLabel,
  ledgerRelationLabel,
  ledgerStatusLabel,
  methodReconstruction,
  modeLabel,
  repositoryPreparation,
  runKindLabel,
  stageLabel,
  statusLabel,
  toolLabel,
} from "./labels.ts";

/**
 * The label vocabulary used to be buried inside `App.tsx`, which made it
 * untestable and duplicated between two files.  These tests pin the wording,
 * because a label is part of what the user reads: a silent drift here would
 * change the interface without changing any behaviour the backend can see.
 */

describe("statusLabel", () => {
  it("names every backend status", () => {
    assert.equal(statusLabel("queued"), "排队中");
    assert.equal(statusLabel("running"), "执行中");
    assert.equal(statusLabel("cancelling"), "正在取消");
    assert.equal(statusLabel("completed"), "已完成");
    assert.equal(statusLabel("cancelled"), "已取消");
    assert.equal(statusLabel("failed"), "执行失败");
    assert.equal(statusLabel("partial_failed"), "部分完成");
  });

  it("falls back to the raw status, then to a waiting label", () => {
    assert.equal(statusLabel("something_new"), "something_new");
    assert.equal(statusLabel(undefined), "等待开始");
    assert.equal(statusLabel(""), "等待开始");
  });
});

describe("toolLabel", () => {
  it("names the known tools", () => {
    assert.equal(toolLabel("search_papers"), "检索论文");
    assert.equal(toolLabel("query_papers"), "检索本地论文库");
  });

  it("falls back to the raw name for an unknown tool", () => {
    assert.equal(toolLabel("mcp__arxiv__search"), "mcp__arxiv__search");
  });

  it("names a nameless tool", () => {
    assert.equal(toolLabel(undefined), "工具调用");
  });
});

describe("runKindLabel and modeLabel", () => {
  it("distinguishes the run kinds", () => {
    assert.equal(runKindLabel("chat"), "对话");
    assert.equal(runKindLabel("research"), "深度研究");
    assert.equal(runKindLabel("daily"), "每日检索");
    assert.equal(runKindLabel("experiment"), "论文复现");
  });

  it("distinguishes the two chat modes", () => {
    assert.equal(modeLabel("chat"), "对话");
    assert.equal(modeLabel("research"), "深度研究");
  });
});

describe("durationLabel", () => {
  it("switches to seconds at one thousand milliseconds", () => {
    assert.equal(durationLabel(999), "999 毫秒");
    assert.equal(durationLabel(1000), "1.0 秒");
    assert.equal(durationLabel(1500), "1.5 秒");
  });

  it("renders nothing for a missing measurement", () => {
    assert.equal(durationLabel(null), "");
    assert.equal(durationLabel(undefined), "");
  });
});

describe("stageLabel", () => {
  it("explains the steering stages", () => {
    assert.equal(stageLabel("steer_queued", "fallback"), "已收到运行中补充，等待下一节点使用");
    assert.equal(stageLabel("steer_consumed", "fallback"), "运行中补充已在下一节点使用");
  });

  it("falls back for an unknown stage", () => {
    assert.equal(stageLabel("other", "fallback"), "other");
    assert.equal(stageLabel(undefined, "fallback"), "fallback");
  });
});

describe("ledger labels", () => {
  it("names the ledger vocabulary", () => {
    assert.equal(ledgerKindLabel("hypothesis"), "假设");
    assert.equal(ledgerStatusLabel("supported"), "有证据支持");
    assert.equal(ledgerRelationLabel("contradicts"), "冲突");
  });

  it("keeps an unknown value visible rather than blanking it", () => {
    assert.equal(ledgerKindLabel("novel"), "novel");
    assert.equal(ledgerStatusLabel("novel"), "novel");
    assert.equal(ledgerRelationLabel("novel"), "novel");
  });
});

describe("daily labels", () => {
  it("names the paper status", () => {
    assert.equal(dailyPaperStatusLabel("new"), "未处理");
    assert.equal(dailyPaperStatusLabel("skipped"), "已跳过");
  });

  it("titles a run by its date", () => {
    assert.equal(
      dailyRunTitle({ created_at: "2026-10-21T08:00:00Z" } as never),
      "每日检索 · 2026-10-21",
    );
    assert.equal(dailyRunTitle({} as never), "每日检索 · 未命名");
  });
});

describe("experiment source derivation", () => {
  it("reports a project with no checked source as not checked", () => {
    const source = experimentCodeSource({ spec: {} } as never);
    assert.equal(source.mode, "not_checked");
    assert.equal(codeSourceLabel(source), "尚未检查");
  });

  it("names a confirmed official repository", () => {
    const source = experimentCodeSource({
      spec: { code_source: { mode: "repository", relationship: "official_confirmed" } },
    } as never);
    assert.equal(codeSourceLabel(source), "官方实现（已确认）");
  });

  it("names a repository that is still unverified", () => {
    const source = experimentCodeSource({
      spec: { code_source: { mode: "repository", relationship: "candidate_unverified" } },
    } as never);
    assert.equal(codeSourceLabel(source), "GitHub 候选（待核验）");
  });

  it("distinguishes the search and reconstruction modes", () => {
    assert.equal(
      codeSourceLabel({ mode: "repository_search", status: "candidates_found" }),
      "已找到候选",
    );
    assert.equal(
      codeSourceLabel({ mode: "repository_search", status: "none" }),
      "未找到候选",
    );
    assert.equal(codeSourceLabel({ mode: "method_reconstruction" }), "按论文方法还原");
  });

  it("treats a malformed preparation payload as absent", () => {
    assert.equal(repositoryPreparation({ spec: {} } as never), null);
    assert.equal(repositoryPreparation({ spec: { repository_preparation: [] } } as never), null);
    assert.equal(methodReconstruction({ spec: {} } as never), null);
    assert.equal(methodReconstruction({ spec: { method_reconstruction: "x" } } as never), null);
  });
});

describe("displayMessages", () => {
  it("gives every message a stable id", () => {
    const messages = displayMessages([
      { role: "user", content: "a" },
      { role: "assistant", content: "b" },
    ] as never);
    assert.equal(messages[0].id, "0-user");
    assert.equal(messages[1].id, "1-assistant");
  });
});

describe("appendRunEvent", () => {
  it("drops token events, which belong to the transcript", () => {
    const events = appendRunEvent([], { type: "token", text: "x" } as never);
    assert.equal(events.length, 0);
  });

  it("drops a repeated status transition", () => {
    const first = appendRunEvent([], { type: "status", status: "running", stage: "tools" } as never);
    const second = appendRunEvent(first, { type: "status", status: "running", stage: "tools" } as never);
    assert.equal(second.length, 1);
  });

  it("keeps a status change", () => {
    const first = appendRunEvent([], { type: "status", status: "running", stage: "tools" } as never);
    const second = appendRunEvent(first, { type: "status", status: "completed", stage: "tools" } as never);
    assert.equal(second.length, 2);
  });

  it("keeps a repeated event whose stage differs", () => {
    const first = appendRunEvent([], { type: "status", status: "running", stage: "tools" } as never);
    const second = appendRunEvent(first, { type: "status", status: "running", stage: "verify" } as never);
    assert.equal(second.length, 2);
  });
});

describe("badcaseCategoryLabel", () => {
  it("names every category", () => {
    assert.equal(badcaseCategoryLabel("retrieval_miss"), "召回遗漏");
    assert.equal(badcaseCategoryLabel("other"), "其他");
  });
});
