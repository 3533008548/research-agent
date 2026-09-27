"""Pluggable embedding backends for the local paper vector store.

The store must never hard-code a model.  It receives an :class:`Embedder` and
relies only on ``dimensions`` / ``max_length`` / ``__call__``, so swapping
all-MiniLM-L6-v2 for Qwen3-Embedding is a configuration change rather than a
code change.

Measured facts behind the defaults (see ``scripts/bench_embedding.py``):

* all-MiniLM-L6-v2 truncates at **256 tokens** — chunks longer than that are
  silently cut, which is why ``max_length`` is part of the contract.
* Chinese tokenises at roughly 1 token/character for this model, so a
  1000-character Chinese chunk loses about three quarters of its content.
"""

from __future__ import annotations

import inspect
import pathlib
import sys
from typing import Any, Protocol, Sequence

# The ONNX backend stores its tokenizer beside the weights, so token counts for
# chunk sizing are available offline.  Loaded lazily and cached.
_MINILM_TOKENIZER_PATH = (
    pathlib.Path.home()
    / ".cache"
    / "chroma"
    / "onnx_models"
    / "all-MiniLM-L6-v2"
    / "onnx"
    / "tokenizer.json"
)
_MINILM_TOKENIZER: Any = None
# False caches "known unusable" so a missing download does not turn into a disk
# probe on every chunk.

# Used when ``rag.embedding.model`` is absent from config.yaml.
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
# Kept in sync with DEFAULT_EMBEDDING_MODEL; only a documentation fallback.
DEFAULT_EMBEDDING_DIMENSIONS = 384


def _load_minilm_tokenizer() -> Any:
    """Load the ONNX tokenizer, returning ``False`` when it is unavailable.

    The weights are downloaded on first use, so a fresh install — or an offline
    CI runner — has no tokenizer file yet.  Counting tokens is only used to size
    chunks, so this must degrade instead of raising.
    """
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return False
    try:
        tokenizer = Tokenizer.from_file(str(_MINILM_TOKENIZER_PATH))
        # tokenizer.json ships with truncation enabled (128 tokens), which would
        # silently cap every count and defeat chunk sizing.
        tokenizer.no_truncation()
        tokenizer.no_padding()
    except Exception:  # noqa: BLE001 - missing weights, corrupt file, no network
        return False
    return tokenizer


class Embedder(Protocol):
    """The contract every embedding backend must satisfy.

    ``__call__`` / ``name`` / ``get_config`` mirror ChromaDB's embedding-function
    protocol so an instance can be handed to a collection directly.
    """

    @property
    def model_name(self) -> str:
        """Human-readable model identifier, used for logging and collection config."""

    @property
    def dimensions(self) -> int:
        """Width of one embedding; determines the ChromaDB collection layout."""

    @property
    def max_length(self) -> int:
        """Token ceiling per input. Longer text is truncated by the model."""

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        """Encode texts into embedding vectors."""

    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        """Encode a search query.

        ChromaDB calls this (not ``__call__``) when embedding query texts, so
        every backend must provide it.
        """

    def tokenize(self, text: str) -> list[int] | None:
        """Token ids for ``text``, or None when the backend cannot tokenize.

        Chunking uses this to size blocks against :attr:`max_length`.  Without
        it the caller falls back to character-based sizing.
        """

    def name(self) -> str:
        """Stable backend name persisted in the collection configuration."""

    def get_config(self) -> dict[str, Any]:
        """Serializable configuration for this backend."""


class MiniLMEmbedder:
    """ChromaDB's built-in ONNX all-MiniLM-L6-v2 (384-d, CPU only).

    This is the historical backend.  It needs no sentence-transformers or
    PyTorch install and stays the safe fallback when a larger model is
    unavailable.
    """

    def __init__(self) -> None:
        from chromadb.utils import embedding_functions

        self._fn = embedding_functions.DefaultEmbeddingFunction()

    @property
    def model_name(self) -> str:
        return "all-MiniLM-L6-v2"

    @property
    def dimensions(self) -> int:
        return 384

    @property
    def max_length(self) -> int:
        # Verified empirically: inputs beyond 256 tokens are discarded.
        return 256

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return self._fn(list(input))

    def tokenize(self, text: str) -> list[int] | None:
        """Token ids from the bundled WordPiece tokenizer (offline, no network).

        Returns ``None`` when the tokenizer cannot be loaded; callers fall back
        to character-based sizing rather than aborting the import.
        """
        global _MINILM_TOKENIZER
        if _MINILM_TOKENIZER is None:
            _MINILM_TOKENIZER = _load_minilm_tokenizer()
        tokenizer = _MINILM_TOKENIZER
        if tokenizer is False:
            return None
        try:
            return tokenizer.encode(text).ids
        except Exception:  # noqa: BLE001 - never let counting break indexing
            return None

    @classmethod
    def name(cls) -> str:
        # ChromaDB registers embedding functions by calling name() on the *class*,
        # so this must not depend on instance state.  "default" is the name
        # ChromaDB gives its built-in function, which keeps existing collections
        # created before this wrapper was introduced readable.
        return "default"

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        return {}

    def __getattr__(self, item: str) -> Any:
        """Delegate ChromaDB protocol helpers to the wrapped function.

        ChromaDB queries through ``embed_query`` and also probes
        ``is_legacy`` / ``validate_config`` / ``default_space``.  Delegating
        them keeps this wrapper a drop-in replacement for the raw function.
        """
        try:
            wrapped = self.__dict__["_fn"]
        except KeyError:
            raise AttributeError(item) from None
        return getattr(wrapped, item)

    @classmethod
    def build_from_config(cls, config: dict[str, Any] | None = None) -> "MiniLMEmbedder":
        return cls()


_REGISTRY: dict[str, type] = {"all-minilm-l6-v2": MiniLMEmbedder, "default": MiniLMEmbedder}


def register_embedder(*names: str):
    """Register a backend under one or more lowercase model names."""

    def wrap(cls: type) -> type:
        for name in names:
            _REGISTRY[name.casefold()] = cls
        return cls

    return wrap


@register_embedder(
    "qwen3-embedding",
    "qwen3-embedding-0.6b",
    "qwen3-embedding-4b",
    "qwen3-embedding-8b",
)
class Qwen3Embedder:
    """Qwen3-Embedding through sentence-transformers.

    Multilingual (100+ languages), 32k context, and Matryoshka-trained, so the
    output can be truncated to a smaller dimension to trade a little quality
    for a lot of storage.

    The weights are downloaded on first use.  If they are unavailable (offline
    host, blocked registry), construction raises and :func:`build_embedder`
    falls back to the built-in MiniLM backend.
    """

    # Native dense width per checkpoint; MRL allows storing fewer dimensions.
    _NATIVE_DIMENSIONS = {
        "0.6b": 1024,
        "4b": 2560,
        "8b": 4096,
    }

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-Embedding-0.6B",
        dimensions: int = 0,
        max_length: int = 8192,
        batch_size: int = 32,
        device: str = "cpu",
        query_instruction: str = "",
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self._model_name = model_name
        self._batch_size = max(1, int(batch_size or 32))
        self._device = device or "cpu"
        self._query_instruction = (query_instruction or "").strip()
        self._max_length = max(256, int(max_length or 8192))

        self._model = SentenceTransformer(
            model_name, device=self._device, trust_remote_code=True
        )
        self._model.max_seq_length = self._max_length

        native = self._native_dimensions()
        # 0 (or anything larger than native) means "use the model's own width".
        self._dimensions = dimensions if 0 < int(dimensions or 0) < native else native

    def _native_dimensions(self) -> int:
        tail = self._model_name.casefold().rsplit("/", 1)[-1]
        for key, value in self._NATIVE_DIMENSIONS.items():
            if key in tail:
                return value
        return 1024

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_length(self) -> int:
        return self._max_length

    def _encode(self, texts: list[str]) -> list[list[float]]:
        kwargs: dict[str, Any] = {
            "batch_size": self._batch_size,
            "convert_to_numpy": True,
            "normalize_embeddings": True,
            "show_progress_bar": False,
        }
        native = self._native_dimensions()
        if self._dimensions < native:
            # Matryoshka truncation: keep the leading slice of the vector.
            kwargs["truncate_dim"] = self._dimensions
        vectors = self._model.encode(list(texts), **kwargs)
        return [[float(value) for value in row] for row in vectors]

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return self._encode(list(input))

    def embed_query(self, input: Sequence[str]) -> list[list[float]]:
        """Encode a search query, optionally with the model's instruction prefix."""
        texts = list(input)
        if self._query_instruction:
            texts = [f"{self._query_instruction}{text}" for text in texts]
        return self._encode(texts)

    def tokenize(self, text: str) -> list[int] | None:
        try:
            return list(self._model.tokenizer.encode(text).ids)
        except Exception:  # noqa: BLE001 - tokenizer shape varies by version
            return None

    @classmethod
    def name(cls) -> str:
        return "qwen3"

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        return {}

    @classmethod
    def build_from_config(cls, config: dict[str, Any] | None = None) -> "Qwen3Embedder":
        return cls(**(config or {}))


def build_embedder(model: str = "", **kwargs: Any) -> Embedder:
    """Create an embedder from a model name.

    Unknown names fall back to the built-in MiniLM backend so a typo in
    config.yaml degrades to the safe model instead of breaking startup.
    Backends only receive the keyword arguments their ``__init__`` accepts, so
    MiniLM can ignore Qwen3-only options such as ``dimensions``.
    """
    requested = (model or DEFAULT_EMBEDDING_MODEL).strip()
    key = requested.casefold()
    cls = _REGISTRY.get(key)
    if cls is None:
        # Allow names such as "Qwen/Qwen3-Embedding-0.6B" to match "qwen3-embedding-0.6b".
        tail = key.rsplit("/", 1)[-1]
        cls = _REGISTRY.get(tail)
    if cls is None:
        for registered, registered_cls in _REGISTRY.items():
            if registered != "default" and registered in key:
                cls = registered_cls
                break
    if cls is None:
        cls = MiniLMEmbedder
    try:
        accepted = set(inspect.signature(cls.__init__).parameters)
    except (TypeError, ValueError):  # pragma: no cover - exotic constructors
        accepted = set()
    options = {k: v for k, v in kwargs.items() if k in accepted}
    try:
        return cls(**options)
    except Exception as exc:  # noqa: BLE001 - backend may be unavailable
        if cls is MiniLMEmbedder:
            raise
        # A heavier backend can be unavailable (offline host, blocked registry).
        # Degrade to the built-in model rather than breaking startup.
        print(
            f"      [Embedding] {requested} 不可用（{type(exc).__name__}），"
            f"回退到 {DEFAULT_EMBEDDING_MODEL}",
            file=sys.stderr,
        )
        return MiniLMEmbedder()
