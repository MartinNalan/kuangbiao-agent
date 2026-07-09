from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mining_qa.kb_build_utils import stable_id  # noqa: E402
from mining_qa.knowledge_store import DEFAULT_DB_PATH, connect, utc_now  # noqa: E402


MINERAL_TERMS = [
    "岩金", "金矿", "方解石", "稀土", "铁", "锰", "铬", "铝土矿", "钨", "锡", "汞", "锑",
    "硫铁矿", "石灰岩", "水泥配料", "盐类", "现代盐湖", "古代固体盐", "深藏卤水",
    "高岭土", "叶蜡石", "耐火黏土", "金属砂矿", "稀有金属", "石膏", "天青石", "硅藻土",
    "钒矿", "蓝晶石", "红柱石", "矽线石", "硅灰石", "透辉石", "透闪石", "长石",
    "煤层气", "页岩气", "油气", "地热", "地下水", "矿泉水", "铀矿", "地浸砂岩型铀矿",
    "压覆矿产资源", "矿产资源", "战略性矿产", "煤炭", "稀土矿", "钨矿",
]


STANDARD_REF_RE = re.compile(r"\b(GB/T|GB|DZ/T|MT/T|EJ/T)\s*[0-9]+(?:\.[0-9]+)*(?:[-—－–]\d{2,4})?\b", re.I)


def init_kg(conn) -> None:
    conn.executescript(
        """
        create table if not exists kg_entities (
          entity_id text primary key,
          entity_type text not null,
          name text not null,
          normalized_name text not null,
          source_id text,
          metadata_json text,
          created_at text not null
        );
        create table if not exists kg_relations (
          relation_id text primary key,
          source_entity_id text not null,
          relation_type text not null,
          target_entity_id text not null,
          evidence_chunk_id text,
          confidence real not null default 1.0,
          metadata_json text,
          created_at text not null
        );
        create index if not exists idx_kg_entities_type_name on kg_entities(entity_type, normalized_name);
        create index if not exists idx_kg_rel_source on kg_relations(source_entity_id, relation_type);
        create index if not exists idx_kg_rel_target on kg_relations(target_entity_id, relation_type);
        """
    )
    conn.execute("delete from kg_relations")
    conn.execute("delete from kg_entities")


def norm(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").upper().replace("—", "-").replace("－", "-").replace("–", "-"))


def entity_id(entity_type: str, name: str, source_id: str | None = None) -> str:
    return stable_id(entity_type, norm(name), source_id if entity_type in {"Clause", "Table"} else "", prefix="kg")


def add_entity(conn, entity_type: str, name: str, source_id: str | None, metadata: dict[str, Any], now: str) -> str:
    eid = entity_id(entity_type, name, source_id)
    conn.execute(
        """
        insert or ignore into kg_entities(entity_id, entity_type, name, normalized_name, source_id, metadata_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (eid, entity_type, name, norm(name), source_id, json.dumps(metadata, ensure_ascii=False), now),
    )
    return eid


def add_relation(conn, src: str, rel: str, dst: str, evidence: str | None, confidence: float, metadata: dict[str, Any], now: str) -> None:
    rid = stable_id(src, rel, dst, evidence, prefix="rel")
    conn.execute(
        """
        insert or ignore into kg_relations(relation_id, source_entity_id, relation_type, target_entity_id, evidence_chunk_id, confidence, metadata_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (rid, src, rel, dst, evidence, confidence, json.dumps(metadata, ensure_ascii=False), now),
    )


def build_kg(db_path: Path) -> dict[str, int]:
    now = utc_now()
    with connect(db_path) as conn:
        init_kg(conn)
        docs = conn.execute("select * from documents").fetchall()
        doc_entities: dict[str, str] = {}
        standard_code_entities: dict[str, str] = {}
        for doc in docs:
            dtype = "Policy" if doc["source_type"] == "official_fulltext" else "Standard"
            eid = add_entity(conn, dtype, doc["title"], doc["document_id"], dict(doc), now)
            doc_entities[doc["document_id"]] = eid
            if doc["standard_no"]:
                code_eid = add_entity(conn, "StandardCode", doc["standard_no"], None, {"document_id": doc["document_id"]}, now)
                standard_code_entities[norm(doc["standard_no"])] = code_eid
                add_relation(conn, eid, "HAS_CODE", code_eid, None, 1.0, {}, now)
            try:
                bib = json.loads(doc["bibliographic_json"] or "{}")
            except json.JSONDecodeError:
                bib = {}
            replaces = bib.get("replaces") or bib.get("替代")
            if replaces:
                rep_eid = add_entity(conn, "StandardCode", str(replaces), None, {}, now)
                add_relation(conn, eid, "REPLACES", rep_eid, None, 0.95, {"raw": replaces}, now)
            for mineral in MINERAL_TERMS:
                if mineral in doc["title"]:
                    mid = add_entity(conn, "Mineral", mineral, None, {}, now)
                    add_relation(conn, eid, "APPLIES_TO_MINERAL", mid, None, 0.9, {"source": "title"}, now)

        chunks = conn.execute(
            """
            select chunk_id, document_id, chunk_type, title, standard_no, section_path, clause_no, text
            from chunks
            where chunk_type in ('clause', 'policy_clause', 'table')
            """
        ).fetchall()
        for chunk in chunks:
            doc_eid = doc_entities.get(chunk["document_id"])
            if not doc_eid:
                continue
            if chunk["chunk_type"] == "table":
                name = chunk["section_path"] or chunk["text"][:80]
                ceid = add_entity(conn, "Table", name, chunk["chunk_id"], {"document_id": chunk["document_id"]}, now)
                add_relation(conn, doc_eid, "HAS_TABLE", ceid, chunk["chunk_id"], 1.0, {}, now)
            else:
                name = chunk["clause_no"] or chunk["section_path"] or chunk["text"][:80]
                ceid = add_entity(conn, "Clause", name, chunk["chunk_id"], {"document_id": chunk["document_id"]}, now)
                add_relation(conn, doc_eid, "HAS_CLAUSE", ceid, chunk["chunk_id"], 1.0, {}, now)
            text = chunk["text"] or ""
            for mineral in MINERAL_TERMS:
                if mineral in text:
                    mid = add_entity(conn, "Mineral", mineral, None, {}, now)
                    add_relation(conn, ceid, "MENTIONS_MINERAL", mid, chunk["chunk_id"], 0.8, {}, now)
                    add_relation(conn, doc_eid, "APPLIES_TO_MINERAL", mid, chunk["chunk_id"], 0.55, {"source": "chunk_text"}, now)
            for ref in STANDARD_REF_RE.findall(text):
                pass
            for ref_match in STANDARD_REF_RE.finditer(text):
                code = ref_match.group(0).replace("—", "-").replace("－", "-").replace("–", "-")
                ref_eid = add_entity(conn, "StandardCode", code, None, {}, now)
                add_relation(conn, doc_eid, "REFERENCES_STANDARD", ref_eid, chunk["chunk_id"], 0.75, {}, now)

        ecount = conn.execute("select count(*) from kg_entities").fetchone()[0]
        rcount = conn.execute("select count(*) from kg_relations").fetchone()[0]
    return {"entities": ecount, "relations": rcount}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build lightweight SQLite knowledge graph for KB.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    print(build_kg(Path(args.db)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
