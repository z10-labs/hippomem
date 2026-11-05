"""The embedding model, and conversion to and from SQLite BLOBs.

Call this module's functions through the module (`embedding.embed(...)`), never
`from hippomem.embedding import embed`: tests patch `embedding.embed` once, and
a `from` import would keep a reference to the real model.
"""

from __future__ import annotations

from typing import Any

import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model: Any = None


def _get_model() -> Any:
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed(text: str) -> list[float]:
    return next(_get_model().embed([text])).tolist()


def normalized(text: str) -> np.ndarray:
    """Unit-length float32 vector, so cosine similarity is a dot product."""
    vec = np.asarray(embed(text), dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
