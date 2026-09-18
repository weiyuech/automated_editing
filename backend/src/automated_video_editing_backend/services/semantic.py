"""Lazy, offline BAAI embeddings for narration meaning, never audio clocks."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from threading import Lock

import numpy as np

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "semantic" / "bge-small-zh-v1.5"
MODEL_PATH = ASSET_DIR / "model_int8.onnx"
TOKENIZER_PATH = ASSET_DIR / "tokenizer.json"
MAX_TOKENS = 256


class BgeOnnxEmbedder:
    """Lazy CPU inference for the bundled 24 MB INT8 BGE model.

    Imports are deliberately lazy. An unpackaged development tree has no model asset, and an
    older installation may not contain ONNX Runtime. Both cases are an ordinary semantic
    fallback, not a backend-startup failure.
    """

    evidence = "bge-small-zh-v1.5-int8"

    def __init__(
        self,
        model_path: Path = MODEL_PATH,
        tokenizer_path: Path = TOKENIZER_PATH,
    ) -> None:
        self.model_path = model_path
        self.tokenizer_path = tokenizer_path
        self._session = None
        self._tokenizer = None
        self._cache: dict[str, np.ndarray] = {}
        self.problem = ""
        self._lock = Lock()

    def similarity(self, left: Sequence[str], right: Sequence[str]) -> np.ndarray | None:
        with self._lock:
            return self._similarity(left, right)

    def _similarity(self, left, right):
        if not left or not right or not self._load():
            return None
        vectors = self._encode(list(dict.fromkeys([*left, *right])))
        if vectors is None:
            return None
        return (
            np.asarray([vectors[text] for text in left])
            @ np.asarray([vectors[text] for text in right]).T
        )

    def _load(self) -> bool:
        if self._session is not None and self._tokenizer is not None:
            return True
        if self.problem:
            return False
        if not self.model_path.is_file() or not self.tokenizer_path.is_file():
            self.problem = "semantic model assets are missing"
            return False
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(self.tokenizer_path))
            tokenizer.enable_truncation(max_length=MAX_TOKENS)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = tokenizer
            return True
        except Exception as exc:  # noqa: BLE001 - optional native runtime may fail many ways
            self.problem = f"semantic model unavailable: {exc.__class__.__name__}"
            return False

    def _encode(self, texts: list[str]) -> dict[str, np.ndarray] | None:
        vectors = {text: self._cache[text] for text in texts if text in self._cache}
        missing = [text for text in texts if text not in vectors]
        try:
            # Bound inference memory even for a script with many short sentences.
            for start in range(0, len(missing), 32):
                batch = missing[start : start + 32]
                encodings = self._tokenizer.encode_batch(batch)
                arrays = {
                    "input_ids": np.asarray([item.ids for item in encodings], dtype=np.int64),
                    "attention_mask": np.asarray(
                        [item.attention_mask for item in encodings], dtype=np.int64
                    ),
                    "token_type_ids": np.asarray(
                        [item.type_ids for item in encodings], dtype=np.int64
                    ),
                }
                wanted = {item.name for item in self._session.get_inputs()}
                output = self._session.run(None, {k: v for k, v in arrays.items() if k in wanted})[
                    0
                ][:, 0, :]
                output /= np.maximum(np.linalg.norm(output, axis=1, keepdims=True), 1e-12)
                vectors.update(zip(batch, output))
            self._cache.update(vectors)
            if len(self._cache) > 1024:
                self._cache = dict(list(self._cache.items())[-1024:])
        except Exception as exc:
            self.problem = f"semantic inference failed: {exc.__class__.__name__}"
            return None
        return vectors
