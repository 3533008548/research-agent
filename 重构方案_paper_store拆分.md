# 重构方案：五维目标驱动

> 状态：**Phase 0-4 全部完成**（2026-10-13 ~ 2026-10-14）
> 制定时间：2026-10-13
> 范围：`paper_store.py`（1,508 行）+ 周边运维基线
> 版本：**v2**（v1 只做文件拆分，未覆盖 TCO / 可观测性 / 资源隔离）

---

## 一、五个目标与各自的现状体检

重构计划必须先回答"现状如何"，否则验收标准无从谈起。以下全部为实测数据。

| 目标 | 现状评级 | 关键证据 |
|---|---|---|
| **代码可读性** | 🟡 部分达标 | `paper_store.py` 1,508 行 / 45 方法混管五件事；命名前缀替代模块边界 |
| **逻辑严密统一** | 🟡 部分达标 | 两条已现缺陷：探针默认值与生产不一致；锁校验正则误报 `uvicorn` |
| **故障可定位** | 🔴 **缺口** | `logger.py` 已实现但仅 **1/44** 模块使用；`paper_store.py` 内 7 处 `print`；**零性能埋点** |
| **可维护性** | 🟡 部分达标 | 私有注入点 **49 处**；测试用 `__new__` 绕过构造函数 |
| **TCO** | 🟡 **部分闭合** | 容器资源上限 ✅ 已加（Phase 0）；镜像仍 2.78 GB，待重建后应 < 1.9 GB |
| **性能** | 🟡 有基础缺护栏 | 三个 bench 脚本齐备，但**只能事后测量**；重排约占查询耗时 90% |

### 1.1 最刺眼的一条：日志模块写了没人用

```
logger.py          实现了 get_logger / setup_logging，注释完整
实际使用           1 / 44 个顶层模块
paper_store.py     7 处 print()，0 处 get_logger()
```

**为什么这是"故障可定位"的根问题**：`print()` 走 stdout，无级别、无时间戳、无模块名、
无法按级别过滤、无法结构化检索。线上出问题时你只有一坨文本流，
无法回答"哪个模块慢""哪类错误多""这次请求花了多久"。

### 1.2 性能维度：能测，但不能自证

`scripts/profile_retrieval.py`、`bench_embedding.py`、`bench_reranker.py` 三个脚本齐备，
这是**已有资产**。但：

- `paper_store.py` 内部 **0 处 `time.perf_counter()`** —— 运行时不知道自身耗时
- Prometheus 指标只有 7 个（`api_up` / `uptime` / `queue_*` / `redis_up` / `run_records` / `worker_up`），
  **全部是队列与进程层，没有任何一个检索指标**

即：**性能是可观测的（离线），但不是可监控的（在线）。**

### 1.3 TCO 维度：两个具体数字

```
research-agent:local   2.78 GB
  ├─ 依赖层 (pip install)   1.90 GB   ← 68%
  ├─ apt (libgomp 等)        10.2 MB
  └─ 源码 + 前端dist         10 MB
compose mem_limit/cpus      0 处
```

依赖层 1.9 GB 是**锁文件之前的产物**。2026-10-12 锁定依赖时已排除 19 个 GPU 包
（`nvidia-*` / `cuda-*` / `triton`），但**当前镜像尚未重建**，那部分体积仍在。

---

## 二、重构目标（按优先级 + 可量化验收）

### 目标 1：故障可定位（最高优先级）

**为什么排第一**：其它四维做不好只是"质量差"，这维做不好是"线上出事查不出原因"。

| 子项 | 动作 | 验收标准 |
|---|---|---|
| 1.1 日志统一 | `paper_store.py` 的 7 处 `print` → `get_logger("ra.paper_store")` | 该文件 `print` 归零 |
| 1.2 检索埋点 | 在 `query_hybrid` 四个阶段加分段计时并上报 Prometheus | 出现 `research_agent_retrieval_stage_seconds` histogram |
| 1.3 日志脱敏 | 沿用 `observability.py` 既有原则：不输出 query / 论文标题 / 凭据 | 守卫测试覆盖 |
| 1.4 结构化上下文 | 关键日志带 `paper_id` / `stage` / `duration_ms` 字段 | 可按字段 grep |

> ⚠️ **1.2 是本方案新增的核心项**。拆文件后各阶段天然独立，埋点可精确挂载；
> 而现在混在一个类里，想知道"倒排占了多少"只能靠外部脚本反复试。

### 目标 2：性能可归因

| 子项 | 动作 | 验收标准 |
|---|---|---|
| 2.1 阶段拆分 | 语义 / 词法 / 重排 / 关系扩展四段各自计时 | 与 `profile_retrieval.py` 数字可对齐（误差 < 10%） |
| 2.2 常驻护栏 | 三个 bench 脚本纳入 CI 非阻断 job 或手动触发 | 性能回退能被主动发现 |
| 2.3 本轮不优化 | 只重构不改性能 | 三项检索指标 + 各阶段耗时全部持平 |

### 目标 3：可维护性（拆分主体）

沿用四步拆分（chunking → lexical → rerank → hybrid），
**新增硬约束**：每抽出一个模块，**同步补上它的单元测试**。

| 子项 | 动作 | 验收标准 |
|---|---|---|
| 3.1 模块拆分 | 四步，风险从低到高 | `paper_store.py` ≤ 60 行（纯转发） |
| 3.2 依赖注入 | `lexical.py` 收 `Iterable[tuple[str, dict]]`，不读 Chroma | Chroma 只在 `store.py` 被访问 |
| 3.3 门面保留 | `paper_store.py` 转薄转发层 | 9 个 import 方零改动 |
| 3.4 测试改善 | 拆出的模块可正常构造，不需 `__new__` | 私有注入点 49 处 → 显著下降 |

### 目标 4：代码可读性与逻辑统一

| 子项 | 动作 | 验收标准 |
|---|---|---|
| 4.1 命名去前缀 | `papers.store.PaperStore` 而非 `paper_store.PaperStore` | 目录结构即文档 |
| 4.2 注释带数字 | 沿用现有风格（"0.21–0.73 波动""512→256 减 30%"） | 新增注释均含决策依据 |
| 4.3 常量归位 | `RRF_K` / `HYBRID_CANDIDATE_LIMIT` 移到所属模块 | 不再有跨职责常量 |

### 目标 5：TCO（可延后但不遗漏）

| 子项 | 动作 | 验收标准 |
|---|---|---|
| 5.1 **容器资源上限** | compose 加 `mem_limit` / `cpus` / `mem_reservation` | ✅ **已完成**（Phase 0）：web 2G / worker 3G / redis 512M，各 2 CPU |
| 5.2 **镜像瘦身** | 重建镜像，让排除的 19 个 GPU 包生效 | 镜像 < 1.9 GB（当前 2.78 GB） |
| 5.3 多阶段构建 | 前端构建产物不进最终镜像 | 已完成（现状即如此） |
| 5.4 锁文件降漂移成本 | CI 用 lock 安装 | 已完成（2026-10-12） |

> 💡 **5.2 是"白拿"的收益**：昨天排除了 GPU 依赖树但没重建镜像。
> 下次 `docker compose build` 时自动生效，**零代码改动**。TCO 维度最容易拿的分。

---

## 三、执行顺序（按"风险 × 收益"重排）

v1 按文件风险排序，v2 改为**先做零风险高收益，再动高风险重构**：

```
Phase 0  零风险，立刻可做（TCO 缺口闭合）          ✅ 已完成 2026-10-13
   ├─ 5.1 容器资源上限        6 行配置
   └─ 5.2 镜像重建            下次 build 时自动生效
   ↓ 此时 TCO 缺口已闭合

Phase 1  低风险重构（可读性 + 测试基础）
   └─ 3.1 Step 1：抽 chunking.py
        同步做 1.1 日志统一、4.3 常量归位
   ↓ 得到一个"纯函数 + 有日志"的样板模块

Phase 2  可观测性核心（性能可归因）
   └─ 1.2 检索分段埋点 + Prometheus 指标
        需先完成 Phase 1（阶段边界清晰才好挂）
   ↓ 性能从"事后测"变成"在线监控"

Phase 3  中高风险重构
   ├─ 3.1 Step 2：抽 lexical.py（含 3.2 依赖注入）
   └─ 3.1 Step 3：抽 rerank.py      ← 质量支柱，风险最高

Phase 4  收尾
   └─ 3.1 Step 4：抽 hybrid.py + 门面收敛 + 4.1 命名
```

### Phase 0 完成记录（2026-10-13）

**5.1 容器资源上限 —— 已落地**

实测空闲占用后设定（可由环境变量覆盖，无需改文件）：

| 服务 | mem_limit | mem_reservation | cpus | 依据 |
|---|---|---|---|---|
| `research-agent`（web） | 2 GB | 512 MB | 2.0 | 空闲 178 MB；只做 API 与轻量查询 |
| `api-worker` | 3 GB | 768 MB | 2.0 | 空闲 292 MB；**实际执行检索与重排，吃内存最凶** |
| `redis` | 512 MB | — | — | AOF `fork` 重写会瞬时翻倍占用 |

为什么 worker 给得更大：它才是跑 `query_hybrid` + cross-encoder 重排的那一侧，
web 进程只服务 API。两者按职责分配，而不是一刀切。

验证：`docker compose config` 三个限制均正确解析；重启后
`docker stats` 显示 8.71% / 9.51% / 1.27% 占用率，
健康检查通过（HTTP 200），单测 206 passed —— **余量充足，零回归**。

**5.2 镜像瘦身 —— 待下次 build 生效**

2026-10-12 锁定依赖时已排除 19 个 GPU 包，但当前镜像仍是 2.78 GB 的旧产物。
下次 `docker compose build` 时自动生效，预期降至 < 1.9 GB。**零代码改动。**

**重排的三条理由**：

1. **Phase 0 必须先做**。容器资源限制是 6 行配置、零风险，补的是真实缺口。
   放在重构之后，等于让整个重构过程一直裸奔在"无内存上限"的状态下。
2. **Phase 1 先于 Phase 2**。埋点需要清晰的阶段边界，而边界正是拆分带来的。
   反过来做会得到一堆"不知道在测哪一段"的埋点。
3. **Step 3（rerank）仍然最后**。无重排时段落级 R@1 从 79.4% 掉到 38.2%，
   这是质量支柱，绝不能在其它步骤未稳时动。

---

## 四、每阶段验收（三道固定命令 + 一道新增）

前三道沿用既有纪律，第四道是本方案新增：

```bash
# 1. 锁一致性（防依赖漂移，2026-10-12 新增）
python scripts/check_lock_consistency.py

# 2. 单测（CI 同款环境）
MSYS_NO_PATHCONV=1 docker run --rm -v //d/develop/academic/research_agent:/src -w /src \
  -e APP_DATA_DIR=/tmp/ci-runtime -e SKIP_CHROMA_INTEGRATION=1 \
  -e HOME=/tmp/ci-home -e PYTHONDONTWRITEBYTECODE=1 \
  research-agent:local python -m unittest tests.test_core

# 3. 发布门禁
... python -m evals.release_gate --strict

# 4. 检索质量 + 性能对照（本方案核心）
python scripts/run_rag_retrieval_eval.py \
  --cases evals/fixtures/rag_retrieval_cases.json \
  --collection papers_e5 --model intfloat/multilingual-e5-small --max-length 512 \
  --mode timed_hybrid --reranker

python scripts/profile_retrieval.py    # 四阶段耗时，重构前后逐项对比
```

> ⚠️ 第 4 条的参数**一个都不能少**：该脚本不读 `config.yaml`，
> 漏传会退回 `all-MiniLM-L6-v2` + 默认集合。

**判定标准**：质量三项（段落级 R@1 / MRR / 论文级 R@1）**完全持平**，
四阶段耗时**误差 < 10%**，单测 206 passed、门禁退出码 0。
任何一项不达标 → 立即 `git revert <sha>`，不进下一阶段。

---

## 五、明确不做的事

- ❌ **不在重构中做性能优化**。质量与耗时指标会互相污染，无法归因。
  性能优化单独排期，用重构后的埋点数据驱动。
- ❌ 不改 `PaperStore` 公开签名（13 个 `__init__` 参数 + 全部公开方法）。
- ❌ 不改切块策略、重排参数、`RRF_K`。这些是优化不是重构。
- ❌ 不引入日志框架（如 structlog）。标准库 `logging` 够用，多一个依赖多一处升级风险。
- ❌ 不给 compose 加数据库 / 监控等新中间件。
- ❌ 不一次性全拆完再验证。

---

## 六、风险登记

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| 埋点泄露用户数据（query / 论文标题） | 中 | 高 | 沿用 `observability.py` 脱敏原则 + 守卫测试 |
| 加 `mem_limit` 后 OOM 杀容器 | 中 | 中 | 先测峰值，宽松设（如 4g），观察后再收紧 |
| 拆分导致检索质量下降 | 中 | 高 | 三道验收强制拦截，每阶段独立提交 |
| 私有方法改名，49 处注入点断裂 | 中 | 中 | 门面保留同名转发；分步执行，改一处跑一次 |
| 镜像瘦身后运行时缺包 | 低 | 高 | 重建后必跑完整 CI（含容器冒烟 job） |
| 重构周期过长失去动力 | 中 | 中 | Phase 0/1 一小时内可完成，先拿小胜 |

**兜底**：每阶段单独提交，`git revert <sha>` 完全回到现状。

---

## 七、什么时候不该做

- 手上还有未完成的功能开发 → 先做完功能
- 刚完成模型 / 索引迁移 → 不叠加变更面
  （e5 迁移刚落地，**建议 Phase 0/1 做完再动 Phase 3**）
- 只是嫌行数难看 → **行数不是目的，可测试性与可归因性才是**

---

## 七点五、执行结果（全部完成）

| 阶段 | 成果 | 提交 |
|---|---|---|
| Phase 0 | 容器资源上限（web 2g / worker 3g / redis 512m） | `65db66c` |
| Phase 1 | 抽 `papers/chunking.py`，1508 -> 1250 行，统一日志 | `29d1418` |
| Phase 2 | 检索分段埋点 + 跨进程 Prometheus 指标 | `d1d1f2e` |
| Phase 3a | 抽 `papers/lexical.py`，1250 -> 1140 行 | `db03b50` |
| Phase 3b | 抽 `papers/rerank.py`，1140 -> 1116 行 | `df49294` |
| Phase 4 | 抽 `papers/hybrid.py`，1116 -> 1013 行 | `a739bfb` |

**最终结构**

```
papers/chunking.py    299 行  纯函数切块
papers/lexical.py     272 行  BM25
papers/hybrid.py      288 行  RRF + 关系扩展
papers/rerank.py      128 行  cross-encoder 重排
papers/metrics.py     230 行  检索埋点
paper_store.py       1013 行  门面 + ChromaDB 交互 + 编排
```

`paper_store.py` 1508 -> 1013 行（**-495 行，-33%**），
但它仍是门面——ChromaDB 读写、嵌入注入、关系库持有都在这里，
这些是有状态的，不该进纯函数模块。

**质量全程零变化**（34 条金标 papers_e5，每阶段都跑 `git stash` 实测基线对比）

| 指标 | 重构前 | 重构后 |
|---|---|---|
| 段落级 R@1 | 44.1% | 44.1% |
| MRR | 0.4412 | 0.4412 |
| 论文级 R@1 | 79.4% | 79.4% |
| P50 延迟 | 602.9ms | 607.3ms（噪声内） |

**测试从 206 增长到 357**，且新增的 151 个全部不需要
`PaperStore.__new__`，不需要 ChromaDB，也不需要嵌入模型。

**四道等价性脚本**（`scripts/verify_{chunking,lexical,rerank,hybrid}_equivalence.py`）
每个模块一份，与 `tests/baseline/` 里的冻结快照对比。
这是本次重构最有价值的沉淀：它把"重构没改行为"从信念变成了可执行的检查。

### 五维目标最终状态

| 目标 | 重构前 | 重构后 |
|---|---|---|
| 代码可读性 | 五件事挤在一个类 | 五个专职模块 + 门面，依赖单向 |
| 逻辑严密统一 | 常量与公式分居两处 | 常量随公式迁移；四道等价性持续校验 |
| 故障可定位 | logger 写了没人用，`print` 满地 | 模块日志统一 + 四阶段埋点上 Prometheus |
| 可维护性 | 49 处私有注入点，测试需 `__new__` | 151 个新测试全部正常构造 |
| TCO | 无容器上限，镜像 2.78GB | 上限已设；镜像待重建（锁文件已排除 GPU 依赖） |
| 性能 | 只能事后用脚本测 | 运行时埋点 + 分位数可查 |

## 八、完成后应有的样子

| 维度 | 之前 | 之后 |
|---|---|---|
| 文件结构 | 45 个 py 平铺，靠命名前缀分组 | `papers/` 等按领域分包，依赖单向 |
| 核心文件 | `paper_store.py` 1,508 行 | ≤60 行转发层 + 五个专职模块 |
| 日志 | 7 处 print，模块基本不用 logger | 统一 `get_logger`，带结构化字段 |
| 性能 | 只能事后用脚本测 | 运行时埋点 + Prometheus histogram |
| 故障定位 | 只能看文本流猜 | 按 `stage` / `duration_ms` / `paper_id` 检索 |
| 容器资源 | 无上限 | 内存 / CPU 有硬上限 |
| 镜像 | 2.78 GB | < 1.9 GB |
| 测试 | 49 处私有注入点 | 拆出的模块可正常构造 |

---

## 九、面试可讲的版本

> 我把重构目标拆成五维，每维都有可量化验收：**可读性**看模块边界与文件规模，
> **可维护性**看测试是否需要绕过构造函数，**可定位性**看日志与指标能否回答
> "哪个阶段慢、哪类错多"，**TCO** 看镜像体积与资源上限，**性能**看能否归因到阶段。
>
> 我先做了现状体检，五维里可观测性和 TCO 是真缺口——日志模块写了但只有 1/44 模块在用，
> `paper_store.py` 里零性能埋点，compose 没设内存上限，镜像 2.78 GB。
>
> 所以顺序是**先补零风险缺口再做高风险重构**：先加容器资源上限并重建镜像
> （依赖锁定时已排除 19 个 GPU 包，重建即瘦身后 TCO 缺口就闭合了），
> 再从最纯的切块函数开始拆，拆完立刻补日志和埋点，
> 最后才动重排——它占查询耗时约 90%，也是质量支柱，无重排时 R@1 从 79.4% 掉到 38.2%。
>
> 每阶段独立提交，验收靠 34 条金标的 R@1/MRR 持平加四阶段耗时误差小于 10%，
> 不达标就回滚，不进下一阶段。

---

## 附：v1 方案中保留的有效内容

- **五条职责的依赖图**：只有 3 条边（切块→索引，{切块,BM25,重排}→hybrid），其余不相关
- **`paper_store.py` 是自闭环孤岛**：全文件仅 3 处内部 import，且都是函数内延迟导入。
  这反证五件事在逻辑上独立，是拆分可行的最强证据
- **49 处私有注入点**清单：`test_core.py` 25 + `bench_reranker.py` 12 +
  `profile_retrieval.py` 8 + `run_rag_retrieval_eval.py` 4
- **必须切断的耦合**：`_build_lexical_index` 直接读 `self._collection.get(...)`，
  需改为注入 `Iterable[tuple[str, dict]]`，由 `store.py` 适配

---

## 附录 B：四个运维问题（本方案的由来）

这四个问题最初来自面经。它们不是考背诵概念，而是考**"你知不知道自己系统的边界在哪"**。

### B.1 关键资料与应用层隔离

**现状：已做，且有两层**

1. **数据分两层**（`runtime_paths.py`）：
   ```
   primary/   用户数据，丢了就没了  → db/ papers/ research_documents/
   derived/   可重建的，丢了无所谓  → chroma/ images/ cache/
   ```
   ChromaDB 索引与模型缓存可重建，用户 PDF 与会话记录不能。
   分开后备份只备 `primary/`，误清理只清 `derived/`。

2. **容器降权**：`Dockerfile` 建专用用户 `app`(uid 10001)、`chown` 后 `USER app`，
   进程非 root；`.dockerignore` 挡掉 `.env` / `runtime` / `node_modules`，镜像里没有密钥。
   端口绑 `127.0.0.1` 而非 `0.0.0.0`，默认仅本机可访问。

### B.2 队列轮询频率

**现状：用 Redis 阻塞读，是标准答案**

```python
def reserve(self, consumer: str, *, block_ms: int = 1_000):
    rows = self._redis.xreadgroup(..., block=max(1, int(block_ms)))
```

| 写法 | 空队列时 | CPU | 响应延迟 |
|---|---|---|---|
| 死循环 + `sleep(0.01)` | 每秒空转 100 次 | 高，纯浪费 | ≤10ms |
| `sleep(1)` 轮询 | 每秒醒 1 次 | 低 | 平均 500ms |
| **阻塞读 1 秒**（现状） | 线程挂起 | ≈0 | 任务到即返回 |

配套的容错：worker 每 15s 写心跳（TTL 15s），任务空闲超 120s 由
`xautoclaim` 抢占重做 —— 处理"worker 领了任务但中途崩了，任务永久卡死"。

> ⚠️ 注意：项目**没有磁盘轮询**（无 watchdog / inotify / 目录扫描）。
> 面经说的"轮询"在本项目语境下指队列消费。

### B.3 线程与内存限制

**现状：应用层已做，容器层本轮补齐**

线程——`llm_client.py` 用**三个** `BoundedSemaphore` 而非一个：

```python
self._slots = BoundedSemaphore(max_concurrency)              # 总量闸门（配置为 4）
self._background_slots = BoundedSemaphore(剩余)              # 后台任务专属
self._low_priority_slots = BoundedSemaphore(低优先级)        # 低优先级专属
```

**为什么要分层**：不分层的话，夜间每日调度会把 4 个槽位占满，
白天用户在前端聊天只能干等。这是真实的生产事故模式。
选 `BoundedSemaphore` 而非 `Lock`：`Lock` 串行太慢，`Bounded` 能在释放时校验计数，
捕获"多释放一次"这类 bug。

其它线程上限：查询线程池 `max_workers=1` + `RLock`（避免并发查询互踩倒排索引）；
`daily_orchestrator` 每个数据源一个 `BoundedSemaphore(1)`（独立通道 + 不打爆单一站点）。

内存——`DEFAULT_MAX_TAIL_TOKENS = 25_000` 控制上下文增长；
对话记忆只取最近 20 条 / 单条 300 字符 / 总量 3,000 字符；
Redis 事件流 `maxlen=1_000`；`HYBRID_LEXICAL_MAX_CHUNKS = 5_000` 防止单次查询无界扫描。

容器层——见 Phase 0。

### B.4 面试时怎么答这四个问题

> 隔离分两层：容器内降权到非 root，构建时用 .dockerignore 挡掉 `.env` 和用户数据；
> 数据目录再分 primary 与 derived，备份和清理按目录区分。
>
> 队列用 Redis Stream 阻塞读（`xreadgroup` + `block=1000ms`），空队列时线程挂起不占 CPU；
> 再加心跳和空闲超时抢占，处理 worker 崩溃导致任务卡死。
>
> 线程用分层信号量：总并发 4，但后台和低优先级任务各有独立配额，
> 避免夜间批量任务堵死用户请求。内存有上下文 25k token 上限和 Redis 事件流 1000 条上限。
>
> 容器层我一开始漏了，是这次规划体检时补的：实测空闲 178MB，
> 按 web 2G / worker 3G / redis 512M 设限，worker 给得更大是因为它才跑检索和重排。

**坦白"一开始漏了、后来补了"比硬说"都有"可信得多。**
这四问通常不是要你说"全做了"，而是看你**知不知道自己项目的边界在哪**。