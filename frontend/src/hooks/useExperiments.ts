import { useState } from "react";

import {
  connectExperimentRepository,
  createExperimentRun,
  deleteExperimentProject,
  getExperimentFile,
  getExperimentProject,
  getRun,
  listExperimentProjects,
  searchExperimentRepositoryCandidates,
  streamRun,
  useExperimentMethodReconstruction,
} from "../api";
import { userFacingError } from "../lib/errors";
import { experimentCodeSource, type RepositoryRelationship } from "../lib/labels";
import type {
  ExperimentFileContent,
  ExperimentProject,
  ExperimentProjectDetail,
  Paper,
} from "../types";
import type { Notice, WorkbenchTab } from "../lib/workspace";

/**
 * The paper-reproduction workspace.
 *
 * Extracted from `App.tsx` without behaviour change.  The hook owns the
 * experiment state and every action that mutates it, so the component no
 * longer has to carry five pieces of state plus eleven handlers.
 *
 * The two cross-domain inputs are explicit: the workbench tab list (an
 * experiment opens as a tab) and the chat draft (the "confirm in chat"
 * action drops an instruction into the composer).  Both are passed in rather
 * than reached for, which keeps this hook free of the app's own state.
 */
export function useExperiments({
  apiKey,
  activeSessionId,
  setNotice,
  setDraft,
  setWorkbenchTabs,
  setActiveWorkbenchTabId,
  closeWorkbenchTab,
}: {
  apiKey: string;
  activeSessionId: string | null;
  setNotice: (notice: Notice) => void;
  setDraft: (value: string) => void;
  setWorkbenchTabs: (updater: (current: WorkbenchTab[]) => WorkbenchTab[]) => void;
  setActiveWorkbenchTabId: (tabId: string) => void;
  closeWorkbenchTab: (tabId: string) => void;
}) {
  const [experiments, setExperiments] = useState<ExperimentProject[]>([]);
  const [experimentStarting, setExperimentStarting] = useState<string | null>(null);
  const [experimentFiles, setExperimentFiles] = useState<Record<string, ExperimentFileContent | null>>({});
  const [repositoryUrlDraft, setRepositoryUrlDraft] = useState("");
  const [repositoryRelationship, setRepositoryRelationship] = useState<Exclude<RepositoryRelationship, "candidate_unverified">>("user_provided");

  async function selectExperimentProject(projectId: string) {
    try {
      const project = await getExperimentProject(apiKey, projectId);
      const tabId = `experiment:${project.project_id}`;
      setWorkbenchTabs((current) => {
        const existing = current.find((tab) => tab.id === tabId);
        if (existing?.kind === "experiment") {
          return current.map((tab) => tab.id === tabId ? { ...tab, title: project.paper_title, project } : tab);
        }
        return [...current, { id: tabId, kind: "experiment", title: project.paper_title, project }];
      });
      setActiveWorkbenchTabId(tabId);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function openExperimentFile(projectId: string, relativePath: string) {
    try {
      const file = await getExperimentFile(apiKey, projectId, relativePath);
      setExperimentFiles((current) => ({ ...current, [projectId]: file }));
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function startPaperReproduction(paper: Paper) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再创建复现实验项目。" });
      }
      return;
    }
    setExperimentStarting(paper.paper_id);
    setNotice({ kind: "info", text: "正在从论文页面级证据生成复现实验项目…" });
    try {
      const started = await createExperimentRun(apiKey, activeSessionId, paper.paper_id);
      if (started.project_id) {
        await selectExperimentProject(started.project_id);
      }
      const controller = new AbortController();
      await streamRun(apiKey, started.stream_url, () => undefined, controller.signal);
      const detail = await getRun(apiKey, started.run_id);
      if (detail.project_id) {
        await selectExperimentProject(detail.project_id);
      }
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed" ? "复现实验项目已生成。" : "复现实验项目需要查看后续待确认项。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function retryExperimentProject(project: ExperimentProjectDetail) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再重试复现实验项目。" });
      }
      return;
    }
    setExperimentStarting(`retry:${project.project_id}`);
    setNotice({ kind: "info", text: "正在重试生成实验规格与代码，上一版文件会保留为修订快照。" });
    try {
      const started = await createExperimentRun(apiKey, activeSessionId, project.paper_id, project.project_id);
      await selectExperimentProject(project.project_id);
      await streamRun(apiKey, started.stream_url, () => undefined, new AbortController().signal);
      const detail = await getRun(apiKey, started.run_id);
      await selectExperimentProject(project.project_id);
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed" ? "复现实验项目已重新生成。" : "重试未完全完成，请查看待确认项。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  function replaceExperimentProject(project: ExperimentProjectDetail) {
    const tabId = `experiment:${project.project_id}`;
    setWorkbenchTabs((current) => current.map((tab) => (
      tab.id === tabId && tab.kind === "experiment"
        ? { ...tab, title: project.paper_title, project }
        : tab
    )));
    setExperimentFiles((current) => {
      const { [project.project_id]: _stale, ...remaining } = current;
      return remaining;
    });
  }

  async function searchExperimentRepositories(project: ExperimentProjectDetail) {
    if (experimentStarting) return;
    setExperimentStarting(`source:${project.project_id}`);
    setNotice({ kind: "info", text: "正在按论文标题检索 GitHub 候选仓库…" });
    try {
      const updated = await searchExperimentRepositoryCandidates(apiKey, project.project_id);
      replaceExperimentProject(updated);
      setExperiments(await listExperimentProjects(apiKey));
      const source = experimentCodeSource(updated);
      setNotice({
        kind: "info",
        text: (source.candidate_status || source.status) === "candidates_found"
          ? "已保存 GitHub 候选。候选不等于官方实现，请核验后再连接。"
          : "未找到候选仓库；这不代表论文一定闭源，可直接提供 GitHub 地址或按论文方法还原。",
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function connectExperimentCodeRepository(
    project: ExperimentProjectDetail,
    repositoryUrl: string,
    relationship: RepositoryRelationship,
  ) {
    const url = repositoryUrl.trim();
    if (!url) {
      setNotice({ kind: "info", text: "请先填写 GitHub 仓库地址。" });
      return;
    }
    if (experimentStarting) return;
    setExperimentStarting(`source:${project.project_id}`);
    setNotice({ kind: "info", text: "正在读取 GitHub 仓库元数据并固定当前 commit…" });
    try {
      const updated = await connectExperimentRepository(apiKey, project.project_id, url, relationship);
      replaceExperimentProject(updated);
      setRepositoryUrlDraft("");
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({ kind: "info", text: "仓库已连接并记录当前 commit；尚未克隆、安装依赖或执行代码。" });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function prepareExperimentRepository(project: ExperimentProjectDetail) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再同步上游仓库。" });
      }
      return;
    }
    setExperimentStarting(`prepare:${project.project_id}`);
    setNotice({ kind: "info", text: "正在固定 commit、克隆并静态分析上游仓库；不会安装依赖或执行代码。" });
    try {
      const started = await createExperimentRun(
        apiKey, activeSessionId, project.paper_id, project.project_id, "prepare_repository",
      );
      await streamRun(apiKey, started.stream_url, () => undefined, new AbortController().signal);
      const detail = await getRun(apiKey, started.run_id);
      await selectExperimentProject(project.project_id);
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed"
          ? "上游仓库已准备完成，请核对依赖、数据与训练入口。"
          : "上游仓库准备未完成，请查看运行中心的错误信息。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  async function startExperimentMethodReconstruction(project: ExperimentProjectDetail) {
    if (!activeSessionId || experimentStarting) {
      if (!activeSessionId) {
        setNotice({ kind: "info", text: "请先新建或选择一个会话，再开始论文方法还原。" });
      }
      return;
    }
    setExperimentStarting(`method:${project.project_id}`);
    setNotice({ kind: "info", text: "正在依据论文页面级证据生成方法还原参考实现；不会下载数据、执行训练或声称复现成功。" });
    try {
      const updated = await useExperimentMethodReconstruction(apiKey, project.project_id);
      replaceExperimentProject(updated);
      const started = await createExperimentRun(
        apiKey, activeSessionId, project.paper_id, project.project_id, "reconstruct_method",
      );
      await streamRun(apiKey, started.stream_url, () => undefined, new AbortController().signal);
      const detail = await getRun(apiKey, started.run_id);
      await selectExperimentProject(project.project_id);
      setExperiments(await listExperimentProjects(apiKey));
      setNotice({
        kind: detail.status === "completed" ? "info" : "error",
        text: detail.answer || (detail.status === "completed"
          ? "已生成论文方法还原参考实现，请先核对假设和待确认项。"
          : "方法还原未完全完成，请查看运行中心和待确认项。"),
      });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    } finally {
      setExperimentStarting(null);
    }
  }

  function prepareExperimentConfirmation(project: ExperimentProjectDetail) {
    const unknowns = Array.isArray(project.spec.unknowns)
      ? project.spec.unknowns.map((item) => `- ${String(item)}`).join("\n")
      : "- （请填写要确认的项目）";
    setDraft(
      `【复现实验项目确认】\nproject_id=${project.project_id}\n\n`
      + "请更新这个论文复现实验项目。以下内容由我确认（请把每项改成明确事实后再发送）：\n"
      + "- （在此替换为你已确认的事实；未确认请删除这一行）\n\n"
      + "以下原待确认项中，只有被上述事实明确解决的条目才可移除（请保持原文）：\n"
      + unknowns
      + "\n\n请只写入我确认的内容；同步更新项目规格、配置和 README，不要执行训练或声称指标已复现。",
    );
    setWorkbenchTabs((current) => current.some((tab) => tab.id === "chat") ? current : [...current, { id: "chat", kind: "chat", title: "对话" }]);
    setActiveWorkbenchTabId("chat");
    setNotice({ kind: "info", text: "确认指令已放入对话框。请补全事实后发送，Agent 才会修改该项目。" });
  }

  async function removeExperimentProject(project: ExperimentProjectDetail) {
    if (!window.confirm(`删除“${project.paper_title}”的复现实验项目？这会同时删除该项目的规格、代码和历史修订文件。`)) {
      return;
    }
    try {
      await deleteExperimentProject(apiKey, project.project_id);
      setExperiments((current) => current.filter((item) => item.project_id !== project.project_id));
      setExperimentFiles((current) => {
        const { [project.project_id]: _removed, ...remaining } = current;
        return remaining;
      });
      closeWorkbenchTab(`experiment:${project.project_id}`);
      setNotice({ kind: "info", text: "复现实验项目及其对应文件已删除。" });
    } catch (error) {
      setNotice({ kind: "error", text: userFacingError(error) });
    }
  }

  async function refreshExperiments() {
    setExperiments(await listExperimentProjects(apiKey));
  }

  return {
    experiments,
    refreshExperiments,
    experimentStarting,
    experimentFiles,
    repositoryUrlDraft,
    repositoryRelationship,
    setRepositoryUrlDraft,
    setRepositoryRelationship,
    selectExperimentProject,
    openExperimentFile,
    startPaperReproduction,
    retryExperimentProject,
    searchExperimentRepositories,
    connectExperimentCodeRepository,
    prepareExperimentRepository,
    startExperimentMethodReconstruction,
    prepareExperimentConfirmation,
    removeExperimentProject,
  };
}
