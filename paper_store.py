"""Compatibility imports for clients using the original paper_store module."""

from papers.chunking import chunk_text, clean_index_text, limit_chunks_to_tokens, split_oversized_text
from papers.store import NoOpStore, PaperStore, PaperStoreProtocol

__all__ = [
    "NoOpStore", "PaperStore", "PaperStoreProtocol",
    "chunk_text", "clean_index_text", "limit_chunks_to_tokens", "split_oversized_text",
]


if __name__ == "__main__":
    from papers.store import _run_demo

    _run_demo()
