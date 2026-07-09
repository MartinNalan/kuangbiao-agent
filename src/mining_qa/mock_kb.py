from typing import Any

from fastapi import FastAPI, Query


app = FastAPI(title="Mock Mining Knowledge Service", version="0.1.0")


MOCK_STANDARDS = [
    {
        "document_id": "mock-gold-001",
        "title": "固体矿产地质勘查规范总则",
        "standard_no": "GB/T 13908-2020",
        "document_type": "national_standard",
        "status": "current",
        "source_type": "local_kb",
        "text_access": "ocr_text",
        "validation_status": "verified",
        "can_answer": True,
        "publish_date": "2020-04-28",
        "implementation_date": "2020-05-01",
        "ingestion_time": "2026-07-09T00:00:00+08:00",
        "url": "mock://standards/gbt-13908-2020",
        "source_platform": "Mock KB",
    },
    {
        "document_id": "mock-calcite-001",
        "title": "方解石矿地质勘查规范",
        "standard_no": "DZ/T 0321-2018",
        "document_type": "industry_standard",
        "status": "current",
        "source_type": "official_visual",
        "text_access": "image_ocr_required",
        "validation_status": "metadata_only",
        "can_answer": False,
        "publish_date": "2018-07-05",
        "implementation_date": "2018-11-01",
        "ingestion_time": "2026-07-09T00:00:00+08:00",
        "url": "mock://standards/dzt-0321-2018",
        "source_platform": "Mock KB",
    },
    {
        "document_id": "mock-placer-gold-001",
        "title": "矿产地质勘查规范 金属砂矿类",
        "standard_no": "DZ/T 0208-2020",
        "document_type": "industry_standard",
        "status": "current",
        "source_type": "local_kb",
        "text_access": "ocr_text",
        "validation_status": "verified",
        "can_answer": True,
        "publish_date": "2020-04-30",
        "implementation_date": "2020-05-01",
        "ingestion_time": "2026-07-09T00:00:00+08:00",
        "url": "mock://standards/dzt-0208-2020",
        "source_platform": "Mock KB",
    },
]


@app.get("/knowledge/health")
async def health() -> dict[str, object]:
    return {"ok": True, "service": "mock-kb"}


@app.post("/knowledge/search")
async def search(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", ""))

    if "沙金" in query or "砂金" in query:
        return {
            "results": [
                {
                    "document_id": "mock-placer-gold-001",
                    "title": "矿产地质勘查规范 金属砂矿类",
                    "standard_no": "DZ/T 0208-2020",
                    "clause_no": None,
                    "page": None,
                    "quote": "标准目录命中：DZ/T 0208-2020《矿产地质勘查规范 金属砂矿类》。",
                    "score": 0.95,
                    "source_type": "local_kb",
                    "text_access": "ocr_text",
                    "validation_status": "verified",
                    "source_platform": "Mock KB",
                    "url": "mock://standards/dzt-0208-2020",
                    "hit_type": ["catalog"],
                }
            ],
            "retrieval": {
                "full_text_hits": 1,
                "vector_hits": 1,
                "graph_hits": 1,
                "web_hits": 0,
            },
            "coverage": {
                "has_clause_level_evidence": False,
                "needs_web_supplement": False,
                "notes": ["Mock KB 命中标准目录。"],
            },
        }

    if "矿体外推" in query:
        return {
            "results": [
                {
                    "document_id": "mock-projection-001",
                    "title": "固体矿产资源量估算规程 第2部分：几何法",
                    "standard_no": "DZ/T 0338.2-2020",
                    "clause_no": "5.4.2",
                    "page": 12,
                    "quote": "5.4.2 相邻的两个工程一个见矿，另一个不见矿时，采用有限外推法，自见矿工程外推工程间距的1/2尖灭。若实际工程间距大于推断资源量工程间距，则按推断资源量工程间距的1/2尖推。",
                    "score": 0.96,
                    "source_type": "local_kb",
                    "text_access": "ocr_text",
                    "validation_status": "verified",
                    "source_platform": "Mock KB",
                    "url": "mock://standards/dzt-0338-2-2020",
                },
                {
                    "document_id": "mock-projection-002",
                    "title": "矿产地质勘查规范 岩金",
                    "standard_no": "DZ/T 0205-2020",
                    "clause_no": "8.3.4.5.2",
                    "page": 39,
                    "quote": "8.3.4.5.2 有限外推：两个工程中一个工程见矿，另一个工程未见矿，两工程间距大于或等于理论工程间距，可按理论工程间距的1/2尖推、1/4平推；如两工程间距小于理论工程间距，则按两工程实际间距1/2尖推、1/4平推。",
                    "score": 0.94,
                    "source_type": "local_kb",
                    "text_access": "ocr_text",
                    "validation_status": "verified",
                    "source_platform": "Mock KB",
                    "url": "mock://standards/dzt-0205-2020",
                },
            ],
            "retrieval": {
                "full_text_hits": 2,
                "vector_hits": 2,
                "graph_hits": 2,
                "web_hits": 0,
            },
            "coverage": {
                "has_clause_level_evidence": True,
                "needs_web_supplement": False,
                "notes": ["Mock KB 命中矿体外推条款级证据。"],
            },
        }

    if "金矿" in query or "基本工程间距" in query:
        return {
            "results": [
                {
                    "document_id": "mock-gold-001",
                    "title": "固体矿产地质勘查规范总则",
                    "standard_no": "GB/T 13908-2020",
                    "clause_no": "附录 B",
                    "page": 42,
                    "quote": "示例条款片段：金矿勘查工程间距应结合矿体规模、形态复杂程度和勘查阶段确定。",
                    "score": 0.93,
                    "source_type": "local_kb",
                    "text_access": "ocr_text",
                    "validation_status": "verified",
                    "source_platform": "Mock KB",
                    "url": "mock://standards/gbt-13908-2020",
                }
            ],
            "retrieval": {
                "full_text_hits": 1,
                "vector_hits": 0,
                "graph_hits": 0,
                "web_hits": 0,
            },
            "coverage": {
                "has_clause_level_evidence": True,
                "needs_web_supplement": False,
                "notes": ["Mock KB 命中条款级证据。"],
            },
        }

    return {
        "results": [],
        "retrieval": {
            "full_text_hits": 0,
            "vector_hits": 0,
            "graph_hits": 0,
            "web_hits": 0,
        },
        "coverage": {
            "has_clause_level_evidence": False,
            "needs_web_supplement": True,
            "notes": ["Mock KB 未命中条款级证据。"],
        },
    }


@app.get("/knowledge/standards")
async def standards(
    q: str | None = None,
    standard_no: str | None = None,
    status: str | None = None,
    text_access: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    items = MOCK_STANDARDS
    if q:
        items = [item for item in items if q in item["title"] or q in str(item.get("standard_no", ""))]
    if standard_no:
        items = [item for item in items if item.get("standard_no") == standard_no]
    if status:
        items = [item for item in items if item.get("status") == status]
    if text_access:
        items = [item for item in items if item.get("text_access") == text_access]

    start = (page - 1) * page_size
    end = start + page_size
    return {
        "items": items[start:end],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": len(items),
        },
    }


@app.post("/knowledge/candidates")
async def create_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "candidate_id": "mock-candidate-001",
        "review_status": payload.get("review_status", "candidate_found"),
    }
