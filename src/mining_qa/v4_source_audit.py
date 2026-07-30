"""Read-only source-audit helpers for the isolated v4 corpus."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote


RETRIEVAL_TABLES = {
    "chunks_fts",
    "chunk_vectors",
    "chunk_embeddings",
    "ann_manifest",
    "kg_entities",
    "kg_relations",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma query_only=on")
    return connection


def sqlite_snapshot(path: Path) -> dict[str, Any]:
    with open_sqlite_readonly(path) as connection:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
            )
        ]
        table_counts = {table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "integrity_check": integrity,
        "table_counts": table_counts,
        "retrieval_artifact_counts": {name: table_counts[name] for name in sorted(RETRIEVAL_TABLES & set(tables))},
    }


def pdf_info(path: Path) -> dict[str, Any]:
    process = subprocess.run(
        ["pdfinfo", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    fields: dict[str, str] = {}
    for line in process.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    pages = int(fields["Pages"])
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "page_count": pages,
        "pdfinfo": fields,
    }


def render_pdf_page(pdf_path: Path, page_no: int, output_path: Path, *, dpi: int = 90) -> None:
    if output_path.exists() and output_path.stat().st_size > 0:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = output_path.with_suffix("")
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            "-r",
            str(dpi),
            "-jpeg",
            "-jpegopt",
            "quality=82",
            "-singlefile",
            str(pdf_path),
            str(prefix),
        ],
        check=True,
        capture_output=True,
    )
    generated = prefix.with_suffix(".jpg")
    if generated != output_path:
        generated.replace(output_path)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"failed to render PDF page {pdf_path}#{page_no}")


def image_metrics(path: Path) -> dict[str, Any]:
    from PIL import Image, ImageChops, ImageStat

    with Image.open(path) as image:
        gray = image.convert("L")
        histogram = gray.histogram()
        pixels = gray.width * gray.height
        dark_pixels = sum(histogram[:230])
        nonwhite_pixels = sum(histogram[:248])
        inverted = ImageChops.invert(gray)
        bbox = inverted.point(lambda value: 255 if value > 12 else 0).getbbox()
        mean_gray = ImageStat.Stat(gray).mean[0]
        return {
            "width": gray.width,
            "height": gray.height,
            "mean_gray": round(mean_gray, 4),
            "dark_pixel_ratio_lt_230": round(dark_pixels / pixels, 8),
            "nonwhite_pixel_ratio_lt_248": round(nonwhite_pixels / pixels, 8),
            "content_bbox": list(bbox) if bbox else None,
        }


def compact_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_identity(value: str | None) -> str:
    text = (value or "").upper().replace("—", "-").replace("–", "-").replace("－", "-")
    return "".join(char for char in text if char.isalnum())


def is_category_divider(text: str) -> bool:
    return bool(re.fullmatch(r"[一二三四五六七八九十]+、.{1,20}(?:矿产类|类)", compact_text(text)))


def physical_page_class(text: str, metrics: dict[str, Any]) -> str:
    compact = compact_text(text)
    if is_category_divider(compact):
        return "category_divider"
    if not compact and metrics["dark_pixel_ratio_lt_230"] < 0.01:
        return "blank_page"
    if "目次" in compact and (compact.count("……") + compact.count("...") >= 2):
        return "table_of_contents"
    if "修改单" in compact and ("实施" in compact or "批准" in compact):
        return "amendment_content"
    if "中华人民共和国" in compact and "标准" in compact and ("发布" in compact or "实施" in compact):
        return "standard_cover"
    if not compact:
        return "scan_artifact_only"
    return "substantive_content"


def identity_present(text: str, standard_no: str | None, title: str) -> bool:
    normalized_text = normalize_identity(text)
    code = normalize_identity(standard_no)
    normalized_title = normalize_identity(title.strip("《》"))
    if code and code in normalized_text:
        return True
    if normalized_title and normalized_title in normalized_text:
        return True
    title_terms = [normalize_identity(term) for term in re.split(r"[、，,：:\s]+", title.strip("《》"))]
    meaningful = [term for term in title_terms if len(term) >= 4]
    return bool(meaningful) and sum(term in normalized_text for term in meaningful) >= min(2, len(meaningful))


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
