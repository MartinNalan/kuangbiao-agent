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
    source_scoped_types = {
        "Clause",
        "Table",
        "GuideSection",
        "ServiceGuide",
        "Attachment",
        "AttachmentSection",
        "MaterialRequirement",
    }
    return stable_id(entity_type, norm(name), source_id if entity_type in source_scoped_types else "", prefix="kg")


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


def add_authority_relations(conn, chunk, ceid: str, now: str) -> None:
    text = chunk["text"] or ""
    if "自然资源部负责本级已颁发勘查许可证或采矿许可证" not in text:
        return
    if "其他由省级自然资源主管部门负责" not in text:
        return

    source_url = chunk["source_ref"]
    quote = (
        "自然资源部负责本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案工作，"
        "其他由省级自然资源主管部门负责。"
    )
    responsibilities = [
        (
            "自然资源部",
            "本级已颁发勘查许可证或采矿许可证的矿产资源储量评审备案",
            1.0,
        ),
        (
            "省级自然资源主管部门",
            "其他矿产资源储量评审备案",
            1.0,
        ),
    ]
    for org, responsibility, confidence in responsibilities:
        org_eid = add_entity(conn, "Organization", org, None, {"source": "policy_clause"}, now)
        resp_eid = add_entity(
            conn,
            "Responsibility",
            responsibility,
            None,
            {
                "document_id": chunk["document_id"],
                "standard_no": chunk["standard_no"],
                "clause_no": chunk["clause_no"],
                "section_path": chunk["section_path"],
                "source_url": source_url,
                "quote": quote,
            },
            now,
        )
        add_relation(
            conn,
            org_eid,
            "RESPONSIBLE_FOR",
            resp_eid,
            chunk["chunk_id"],
            confidence,
            {
                "document_id": chunk["document_id"],
                "standard_no": chunk["standard_no"],
                "clause_no": chunk["clause_no"],
                "section_path": chunk["section_path"],
                "source_url": source_url,
                "quote": quote,
                "source_policy": "自然资源部关于深化矿产资源管理改革若干事项的意见",
            },
            now,
        )
        add_relation(
            conn,
            ceid,
            "STATES_RESPONSIBILITY",
            resp_eid,
            chunk["chunk_id"],
            confidence,
            {"quote": quote, "source_url": source_url},
            now,
        )


def guide_matter_name(title: str) -> str:
    return re.sub(r"(?:临时)?服务指南$", "", title or "").strip() or title


def material_name_from_row(row: dict[str, Any], headers: list[str]) -> str:
    for key, value in row.items():
        clean_key = re.sub(r"\s+|\*", "", str(key))
        if any(marker in clean_key for marker in ("材料名称", "提交材料名称", "申请材料")):
            return str(value or "").strip()
    for key in headers[1:]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def add_service_guide_relations(conn, chunk, guide_eid: str, section_eid: str, now: str) -> None:
    section = chunk["section_path"] or ""
    text = (chunk["text"] or "").strip()
    common = {
        "document_id": chunk["document_id"],
        "section_path": section,
        "source_url": chunk["source_ref"],
    }

    if chunk["chunk_type"] == "service_guide_section":
        add_relation(conn, guide_eid, "HAS_SECTION", section_eid, chunk["chunk_id"], 1.0, common, now)

    if section == "适用范围" and text:
        matter = guide_matter_name(chunk["title"] or "")
        matter_eid = add_entity(conn, "Matter", matter, None, {**common, "scope": text}, now)
        add_relation(conn, guide_eid, "APPLIES_TO", matter_eid, chunk["chunk_id"], 1.0, common, now)
    elif section == "受理机构" and text:
        organization = text.rstrip("。；; ")
        org_eid = add_entity(conn, "Organization", organization, None, common, now)
        add_relation(conn, guide_eid, "ACCEPTED_BY", org_eid, chunk["chunk_id"], 1.0, common, now)
    elif section == "决定机构" and text:
        organization = text.rstrip("。；; ")
        org_eid = add_entity(conn, "Organization", organization, None, common, now)
        add_relation(conn, guide_eid, "DECIDED_BY", org_eid, chunk["chunk_id"], 1.0, common, now)
    elif section == "办结时限" and text:
        duration_eid = add_entity(conn, "Duration", text, None, common, now)
        add_relation(conn, guide_eid, "HAS_TIME_LIMIT", duration_eid, chunk["chunk_id"], 1.0, common, now)

    if chunk["chunk_type"] != "table" or not chunk["table_json"]:
        return
    try:
        table = json.loads(chunk["table_json"])
    except json.JSONDecodeError:
        return
    headers = [str(header) for header in (table.get("headers") or [])]
    for row in table.get("rows") or []:
        if not isinstance(row, dict):
            continue
        material = material_name_from_row(row, headers)
        if not material:
            continue
        material_eid = add_entity(conn, "Material", material, None, common, now)
        add_relation(
            conn,
            guide_eid,
            "REQUIRES_MATERIAL",
            material_eid,
            chunk["chunk_id"],
            1.0,
            {**common, "row": row},
            now,
        )


def add_policy_attachment_material_relation(conn, chunk, attachment_eid: str, requirement_eid: str, now: str) -> None:
    if chunk["chunk_type"] != "application_material_row" or not chunk["table_json"]:
        return
    try:
        data = json.loads(chunk["table_json"])
    except json.JSONDecodeError:
        return
    material = str(data.get("material_name") or "").strip()
    if not material:
        return
    metadata = {
        "document_id": chunk["document_id"],
        "application_key": data.get("application_key"),
        "application_type": data.get("section_path"),
        "sequence": data.get("sequence"),
        "marker": data.get("marker"),
        "requirement": data.get("requirement"),
        "source_url": chunk["source_ref"],
    }
    material_eid = add_entity(conn, "Material", material, None, metadata, now)
    add_relation(
        conn,
        attachment_eid,
        "REQUIRES_MATERIAL",
        material_eid,
        chunk["chunk_id"],
        1.0,
        metadata,
        now,
    )
    add_relation(
        conn,
        requirement_eid,
        "SPECIFIES_MATERIAL",
        material_eid,
        chunk["chunk_id"],
        1.0,
        metadata,
        now,
    )


def add_policy_attachment_document_relations(conn, docs, doc_entities: dict[str, str], now: str) -> None:
    for doc in docs:
        if doc["document_type"] != "policy_attachment":
            continue
        attachment_eid = doc_entities.get(doc["document_id"])
        if not attachment_eid:
            continue
        try:
            trace = json.loads(doc["source_trace_json"] or "{}")
        except json.JSONDecodeError:
            trace = {}
        parent_document_id = str(trace.get("parent_document_id") or "")
        parent_eid = doc_entities.get(parent_document_id)
        relationship_metadata = {
            "attachment_url": doc["official_url"],
            "parent_document_id": parent_document_id,
            "parent_standard_no": trace.get("parent_standard_no"),
            "parent_url": trace.get("parent_url"),
        }
        if parent_eid:
            add_relation(
                conn,
                attachment_eid,
                "ATTACHMENT_OF",
                parent_eid,
                None,
                1.0,
                relationship_metadata,
                now,
            )
        matter_eid = add_entity(
            conn,
            "Matter",
            "采矿权新立、延续、变更、注销申请资料",
            None,
            relationship_metadata,
            now,
        )
        add_relation(
            conn,
            attachment_eid,
            "IMPLEMENTS_MATERIAL_LIST_FOR",
            matter_eid,
            None,
            1.0,
            relationship_metadata,
            now,
        )
        for link in trace.get("service_guide_links") or []:
            if not isinstance(link, dict):
                continue
            guide_eid = doc_entities.get(str(link.get("document_id") or ""))
            if not guide_eid:
                continue
            add_relation(
                conn,
                attachment_eid,
                "SUPPORTS_GUIDE",
                guide_eid,
                None,
                0.95,
                {
                    **relationship_metadata,
                    "guide_title": link.get("title"),
                    "guide_url": link.get("source_url"),
                    "guide_url_date": link.get("url_date"),
                    "application_key": link.get("application_key"),
                    "application_section": link.get("application_section"),
                    "relationship_scope": link.get("relationship_scope"),
                    "conflict_policy": link.get("conflict_policy"),
                },
                now,
            )


def build_kg(db_path: Path) -> dict[str, int]:
    now = utc_now()
    with connect(db_path) as conn:
        init_kg(conn)
        docs = conn.execute("select * from documents").fetchall()
        doc_entities: dict[str, str] = {}
        document_types: dict[str, str] = {}
        standard_code_entities: dict[str, str] = {}
        for doc in docs:
            if doc["document_type"] == "policy_attachment":
                dtype = "Attachment"
            elif doc["document_type"] in {"service_guide", "administrative_service_guide"}:
                dtype = "ServiceGuide"
            else:
                dtype = "Policy" if doc["source_type"] == "official_fulltext" else "Standard"
            eid = add_entity(conn, dtype, doc["title"], doc["document_id"], dict(doc), now)
            doc_entities[doc["document_id"]] = eid
            document_types[doc["document_id"]] = doc["document_type"]
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
            select chunk_id, document_id, chunk_type, title, standard_no, section_path, clause_no,
                   text, table_json, source_ref, validation_status
            from chunks
            where chunk_type in (
              'clause', 'policy_clause', 'service_guide_section', 'attachment_overview',
              'application_material_section', 'application_material_row', 'table'
            )
              and validation_status != 'empty_source_section'
            """
        ).fetchall()
        for chunk in chunks:
            doc_eid = doc_entities.get(chunk["document_id"])
            if not doc_eid:
                continue
            is_service_guide = document_types.get(chunk["document_id"]) in {
                "service_guide",
                "administrative_service_guide",
            }
            is_policy_attachment = document_types.get(chunk["document_id"]) == "policy_attachment"
            if chunk["chunk_type"] == "table":
                name = chunk["section_path"] or chunk["text"][:80]
                ceid = add_entity(conn, "Table", name, chunk["chunk_id"], {"document_id": chunk["document_id"]}, now)
                add_relation(conn, doc_eid, "HAS_TABLE", ceid, chunk["chunk_id"], 1.0, {}, now)
            elif is_service_guide:
                name = chunk["section_path"] or chunk["text"][:80]
                ceid = add_entity(
                    conn,
                    "GuideSection",
                    name,
                    chunk["chunk_id"],
                    {"document_id": chunk["document_id"]},
                    now,
                )
            elif is_policy_attachment:
                name = chunk["section_path"] or chunk["text"][:80]
                entity_type = (
                    "MaterialRequirement"
                    if chunk["chunk_type"] == "application_material_row"
                    else "AttachmentSection"
                )
                ceid = add_entity(
                    conn,
                    entity_type,
                    name,
                    chunk["chunk_id"],
                    {"document_id": chunk["document_id"]},
                    now,
                )
                relation_type = "HAS_REQUIREMENT" if entity_type == "MaterialRequirement" else "HAS_SECTION"
                add_relation(conn, doc_eid, relation_type, ceid, chunk["chunk_id"], 1.0, {}, now)
            else:
                name = chunk["clause_no"] or chunk["section_path"] or chunk["text"][:80]
                ceid = add_entity(conn, "Clause", name, chunk["chunk_id"], {"document_id": chunk["document_id"]}, now)
                add_relation(conn, doc_eid, "HAS_CLAUSE", ceid, chunk["chunk_id"], 1.0, {}, now)
            if is_service_guide:
                add_service_guide_relations(conn, chunk, doc_eid, ceid, now)
            if is_policy_attachment:
                add_policy_attachment_material_relation(conn, chunk, doc_eid, ceid, now)
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
            add_authority_relations(conn, chunk, ceid, now)

        add_policy_attachment_document_relations(conn, docs, doc_entities, now)

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
