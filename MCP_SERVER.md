# MCP Server（只读学术检索）

把本项目**已有**的检索能力通过 [Model Context Protocol](https://modelcontextprotocol.io)
暴露给外部 MCP 客户端（Claude Desktop、Cursor、其他 Agent 等）。

- 传输：**stdio**（本地单用户场景无需端口与鉴权，客户端自行管理进程生命周期）
- 启动：`python mcp_server.py`
- 公开工具：**只读**，共 5 个

---

## 公开的工具

| 工具 | 作用 | 依赖 |
|---|---|---|
| `search_papers` | 公开元数据检索（OpenAlex / arXiv），走去重与相关性门控 | 网络 |
| `query_papers` | 本地混合检索（语义 + 词法 + RRF + 重排），返回带页码的原文段落 | 本地向量库 |
| `list_papers` | 已下载到本地的论文（文件名、体积、修改时间） | 本地文件 |
| `list_indexed_papers` | 已进入向量库、可被检索的论文及块数 | 本地向量库 |
| `retrieval_metrics` | 本进程检索各阶段的聚合耗时（语义/词法/融合/重排） | 无 |

### 为什么不暴露写工具

`read_pdf` 会写入向量库，`save_paper_relation` / `apply_research_document_patch`
会修改用户数据。MCP Server 是一个**对外的公开面**：它执行的写操作没有配额、
没有审计、也没有质量门控。这些能力留在应用内部。

### 为什么返回结构化数据而不是文本

内部工具 handler 返回的是给**模型在对话里读**的 Markdown。而 MCP 客户端是
**程序**，所以这一层直接调用 handler 底层相同的函数，返回它们原生的 dict：

```
tools/search.py::handle_query_papers   → 渲染成 Markdown
        ↓ 同一个 query_hybrid
mcp_server.py::RetrievalService         → 返回 {title, page, section, text, reranker_score}
```

这样既避免了解析 Markdown 的脆弱性（改一次格式串就坏），
也保证**只有一份检索实现** —— 不存在"MCP 版排序"和"应用版排序"两份可能漂移的代码。

---

## 在 MCP 客户端里配置

以 Claude Desktop 为例（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "research-agent": {
      "command": "python",
      "args": ["D:/develop/academic/research_agent/mcp_server.py"],
      "env": {
        "PYTHONPATH": "D:/develop/academic/research_agent",
        "MCP_LOG_LEVEL": "WARNING"
      }
    }
  }
}
```

> ⚠️ **建议跑在容器内**。本地裸跑会触发 ONNX 图的重复加载与优化
> （约 530 ms/次，见项目经验沉淀），且 Windows 上HF 模型的
> symlink 缓存读不了。容器内模型已预热。

---

## 三个容易踩的坑（已在代码里处理）

**① stdout 是协议通道，不是日志通道。**
本项目到处是 `print()`（`research_agent.py` 的进度横幅、ChromaDB 的输出、
依赖的告警）。stdio 传输用 `sys.stdout.buffer` 做 JSON-RPC 分帧，
**一个 print 就会往协议流里注入非 JSON**，客户端报的却是解析错误，
完全看不出真正原因。

处理：启动时**先把真正的 stdout 留给传输层**，再把 `sys.stdout` 指向 stderr。

⚠️ 注意顺序不能反。SDK 是在**启动时**才去读 `sys.stdout`（不是 import 时），
所以先重定向会把响应发到 stderr —— 表现为**客户端永远卡在初始化**，
不报错、不超时退出，是最难查的一类故障。
（见 `mcp_server.py::_reserve_transport_stdout` 与 `_run_stdio_with_reserved_stdout`）

**② 必须在事件循环启动前构建论文库。**
这是最隐蔽的一个坑，症状极像"网络慢"：

```
客户端：握手成功 ✅  →  调用 query_papers → 永久无响应，无任何报错
```

实测数据（同一台机器、同一份语料）：

| 构建时机 | 结果 |
|---|---|
| 在事件循环**内**（懒加载，第一次工具调用时才建） | **死锁**，15 分钟无响应 |
| 在 `anyio.run()` **之前**（启动时预热） | 22 秒完成，之后 query 2.8 秒返回 |

所以 `main()` 里显式调 `_warm_store(service)`。这不只是性能优化——
它把 22 秒开销从"客户端第一次调用时的神秘等待"挪到"启动时的一次性等待"。

> ⚠️ **一个被推翻的假设，留作记录**：我一开始判断是"stderr 管道被 `tqdm`
> 进度条写满导致阻塞"，为此加了 stderr 限流和进度条静音。实测 stderr 只有
> **496 字节**，那个假设是错的。真正的分界线是"在事件循环内构建"还是
> "在事件循环外构建"。→ **排除法也要讲证据**，理论合理不等于结论成立。

**③ `-> dict` 会让结构化输出退化成文本。**
FastMCP 依据返回类型注解决定是否生成 output schema。裸 `-> dict`
**不会**生成，于是结果只以 JSON 字符串形式到达客户端，
`structuredContent` 是 `None` —— 而那正是程序化客户端唯一会读的字段。

所以全部工具都标注 `-> dict[str, Any]`。测试里有一条
"premise check"锁住这个行为：`test_bare_dict_annotation_would_lose_structured_output`。

---

## 测试

```
python -m unittest tests.test_mcp_server   # 29 例：工具面、结构化输出、边界、stdio安全、预热顺序
python -m unittest tests.test_mcp_stdio    # 4 例：真实起进程、走 stdio 协议
```

`test_mcp_stdio` 会**真的拉起子进程**、用官方 `ClientSession` 握手并调用工具。
这是唯一能抓到协议层故障的方式：stdout 横幅、握手失败、返回值无法解码、
store 在事件循环内构建导致死锁，**全部能通过所有单元测试**。

其中 `test_store_backed_tool_answers_over_stdio` 是专门为坑②加的：
单元测试全绿时它会挂住，因为死锁只在真实进程 + 真实协议下才发生。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_LOG_LEVEL` | `INFO` | MCP SDK 日志级别。诊断信息走 **stderr**，不影响协议 |
| `APP_DATA_DIR` | — | 运行时目录，与应用共用同一个卷才能读到同一份论文库 |