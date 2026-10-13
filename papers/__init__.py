"""论文域：切块、词法检索、重排、混合编排与存储门面。

按依赖方向分包，上层可以依赖下层，反向不行：

    store  ──▶  hybrid  ──▶  lexical / rerank
                        └─▶  chunking（纯函数，零依赖）

各模块职责：

- :mod:`papers.chunking`  分块策略（纯函数，无IO、可独立测试）
- :mod:`papers.lexical`   BM25 倒排与词法打分
- :mod:`papers.rerank`    cross-encoder 重排
- :mod:`papers.hybrid`    语义 + 词法融合、关系扩展
- :mod:`papers.store`     :class:`PaperStore` 门面（对外契约）

本包处于重构过程中：目前仅 ``chunking`` 已抽出，``paper_store`` 仍是兼容
转发层。待其余模块迁移完毕后，``paper_store`` 会退化为纯转发并最终废弃。

现在只对外暴露门面，避免调用方依赖尚在迁移中的内部模块。
"""

from __future__ import annotations

__all__: list[str] = []