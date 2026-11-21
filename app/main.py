"""口述史转录稿勘误供数 API（FastAPI + SQLite）。

- POST   /documents                                  导入正文与实体标注
- GET    /documents                                  文档列表
- GET    /documents/{doc_id}                         版本查询：正文 + 有效标注区间 + 待复核标注
- POST   /documents/{doc_id}/corrections             应用一笔勘误，返回勘误号
- POST   /documents/{doc_id}/corrections/{n}/retract 按号撤回（冲突则 409 并保留现版）
- GET    /documents/{doc_id}/corrections             勘误流水
- GET    /documents/{doc_id}/entities                实体检索（kind / q 过滤）
- GET    /documents/{doc_id}/entities/{number}       单个实体
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import db, service


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="口述史转录稿勘误供数 API", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 事务辅助

def _write(fn, *args):
    """写操作：BEGIN IMMEDIATE 保证读写串行，异常回滚。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = fn(conn, *args)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read(fn, *args):
    """读操作：显式读事务，保证同一请求内多次查询落在同一版本快照上。"""
    conn = db.connect()
    try:
        conn.execute("BEGIN DEFERRED")
        return fn(conn, *args)
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------- 模型

class EntityIn(BaseModel):
    number: Optional[int] = Field(default=None, description="实体编号，缺省自动分配")
    kind: Literal["person", "place"]
    name: str = Field(min_length=1)
    start: int = Field(ge=0, description="原文区间起点（含）")
    end: int = Field(ge=0, description="原文区间终点（不含）")


class ImportIn(BaseModel):
    text: str = Field(description="转录稿正文")
    entities: list[EntityIn] = Field(default_factory=list)


class CorrectionIn(BaseModel):
    start: int = Field(ge=0, description="当前版区间起点（含）")
    end: int = Field(ge=0, description="当前版区间终点（不含）")
    text: str = Field(default="", description="替换文本；区间为空即插入，文本为空即删除")


# ---------------------------------------------------------------- 异常映射

@app.exception_handler(service.NotFoundError)
async def not_found_handler(request, exc):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(service.ValidationError)
async def validation_handler(request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(service.ConflictError)
async def conflict_handler(request, exc: service.ConflictError):
    # 撤回被冲突阻断：返回冲突勘误号，现版保持不变
    return JSONResponse(
        status_code=409,
        content={"detail": str(exc), "conflicts": exc.conflicts},
    )


# ---------------------------------------------------------------- 路由

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/documents", status_code=201)
def import_document(payload: ImportIn):
    return _write(service.import_document, payload.text,
                  [e.model_dump() for e in payload.entities])


@app.get("/documents")
def documents():
    return _read(service.list_documents)


@app.get("/documents/{doc_id}")
def get_version(doc_id: int):
    """版本查询：同一版返回正文、有效标注当前区间、待复核标注。"""
    return _read(service.get_version, doc_id)


@app.post("/documents/{doc_id}/corrections", status_code=201)
def apply_correction(doc_id: int, payload: CorrectionIn):
    return _write(service.apply_correction, doc_id,
                  payload.start, payload.end, payload.text)


@app.post("/documents/{doc_id}/corrections/{number}/retract")
def retract_correction(doc_id: int, number: int):
    return _write(service.retract_correction, doc_id, number)


@app.get("/documents/{doc_id}/corrections")
def corrections(doc_id: int):
    return _read(service.list_corrections, doc_id)


@app.get("/documents/{doc_id}/entities")
def entities(doc_id: int, kind: Optional[Literal["person", "place"]] = None,
             q: Optional[str] = None):
    return _read(service.search_entities, doc_id, kind, q)


@app.get("/documents/{doc_id}/entities/{number}")
def entity(doc_id: int, number: int):
    items = _read(service.search_entities, doc_id, None, None)
    for item in items:
        if item["number"] == number:
            return item
    raise HTTPException(status_code=404, detail=f"实体编号不存在: {number}")
