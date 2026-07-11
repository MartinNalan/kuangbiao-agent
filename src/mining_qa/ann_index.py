from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

try:
    import numpy as np
    from usearch.index import Index
except ImportError:  # pragma: no cover
    np = None
    Index = None


@dataclass(frozen=True)
class AnnManifest:
    model: str
    dimensions: int
    count: int
    max_updated_at: str
    chunk_ids: tuple[str, ...]


class DenseAnnIndex:
    def __init__(self, index_path: Path, manifest_path: Path):
        self.index_path = index_path
        self.manifest_path = manifest_path
        self._index: Any | None = None
        self._manifest: AnnManifest | None = None
        self._signature: tuple[int, int] | None = None
        self._lock = Lock()

    @property
    def available(self) -> bool:
        return np is not None and Index is not None and self.index_path.exists() and self.manifest_path.exists()

    def manifest(self) -> AnnManifest | None:
        self._ensure_loaded()
        return self._manifest

    def search(self, vector: list[float], count: int) -> list[tuple[str, float]]:
        self._ensure_loaded()
        if self._index is None or self._manifest is None or np is None or not vector:
            return []
        limit = max(1, min(int(count), self._manifest.count))
        query = np.asarray(vector, dtype=np.float32)
        if query.ndim != 1 or query.shape[0] != self._manifest.dimensions:
            return []
        matches = self._index.search(query, count=limit)
        results: list[tuple[str, float]] = []
        for key, distance in zip(matches.keys.tolist(), matches.distances.tolist(), strict=False):
            label = int(key)
            if label < 0 or label >= len(self._manifest.chunk_ids):
                continue
            similarity = max(-1.0, min(1.0, 1.0 - float(distance)))
            results.append((self._manifest.chunk_ids[label], similarity))
        return results

    def _ensure_loaded(self) -> None:
        if not self.available:
            self._index = None
            self._manifest = None
            self._signature = None
            return
        signature = (self.index_path.stat().st_mtime_ns, self.manifest_path.stat().st_mtime_ns)
        if self._index is not None and self._manifest is not None and self._signature == signature:
            return
        with self._lock:
            if self._index is not None and self._manifest is not None and self._signature == signature:
                return
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest = AnnManifest(
                model=str(payload["model"]),
                dimensions=int(payload["dimensions"]),
                count=int(payload["count"]),
                max_updated_at=str(payload.get("max_updated_at") or ""),
                chunk_ids=tuple(str(value) for value in payload["chunk_ids"]),
            )
            if manifest.count != len(manifest.chunk_ids):
                raise ValueError("ANN manifest count does not match chunk ID mapping")
            self._index = Index(path=self.index_path, view=True)
            self._manifest = manifest
            self._signature = signature


_INDEXES: dict[tuple[str, str], DenseAnnIndex] = {}
_INDEXES_LOCK = Lock()


def get_ann_index(index_path: str | Path, manifest_path: str | Path) -> DenseAnnIndex:
    key = (str(Path(index_path).resolve()), str(Path(manifest_path).resolve()))
    with _INDEXES_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = DenseAnnIndex(Path(key[0]), Path(key[1]))
            _INDEXES[key] = index
        return index
