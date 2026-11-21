"""业务操作层（框架无关）：导入、勘误、撤回、版本查询、实体检索。

所有写操作在单个事务内完成；版本查询在一次读事务内取正文与标注，
保证“从同一版返回”。
"""

from __future__ import annotations

import sqlite3

from . import db, engine


class NotFoundError(Exception):
    pass


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    def __init__(self, message: str, conflicts: list[int]):
        super().__init__(message)
        self.conflicts = conflicts


# ---------------------------------------------------------------- 导入

def import_document(conn: sqlite3.Connection, text: str, entities: list[dict]) -> dict:
    """导入正文及带编号、按字符区间标出的人名/地名。"""
    used: set[int] = set()
    for ent in entities:
        n = ent.get("number")
        if n is not None:
            if n in used:
                raise ValidationError(f"实体编号重复: {n}")
            used.add(n)

    assigned: list[dict] = []
    next_n = 1
    for ent in entities:
        ent = dict(ent)
        if ent.get("number") is None:
            while next_n in used:
                next_n += 1
            ent["number"] = next_n
            used.add(next_n)
        s, t = ent["start"], ent["end"]
        if not (0 <= s < t <= len(text)):
            raise ValidationError(
                f"实体 {ent['number']} 区间 [{s}, {t}) 超出正文范围或为空")
        assigned.append(ent)

    cur = conn.execute(
        "INSERT INTO documents (original_text) VALUES (?)", (text,))
    doc_id = cur.lastrowid
    conn.executemany(
        'INSERT INTO entities (document_id, number, kind, name, start, "end")'
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(doc_id, e["number"], e["kind"], e["name"], e["start"], e["end"])
         for e in assigned],
    )
    db.save_pieces(conn, doc_id, [engine.piece("orig", None, 0, len(text))])
    return {"document_id": doc_id, "revision": 0, "entity_count": len(assigned)}


# ---------------------------------------------------------------- 勘误

def apply_correction(conn: sqlite3.Connection, doc_id: int,
                     s: int, e: int, text: str) -> dict:
    """按当前版把 [s, e) 替换为 text，返回勘误号。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    total = engine.total_length(pieces)
    if not (0 <= s <= e <= total):
        raise ValidationError(f"区间 [{s}, {e}) 超出当前版长度 {total}")
    if s == e and not text:
        raise ValidationError("空操作：区间为空且未提供替换文本")

    number = db.next_correction_number(conn, doc_id)
    kind = "insert" if s == e else ("delete" if not text else "replace")

    new_pieces, deleted, conflicts = engine.apply_edit(pieces, s, e, text, number)
    deleted_text = engine.render(deleted, doc["original_text"],
                                 db.correction_texts(conn, doc_id))

    # 标注受影响判定：用应用前的片段表（与 [s, e) 同一坐标系）
    effects = []
    for ent in _entities(conn, doc_id):
        ca, cb = engine.current_range(pieces, ent["start"], ent["end"])
        if engine.edit_touches_span(s, e, bool(text), ca, cb):
            effects.append(ent["id"])

    conn.execute(
        "INSERT INTO corrections"
        " (document_id, number, kind, text, range_start, range_end,"
        "  deleted_text, deleted_pieces)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, number, kind, text, s, e, deleted_text, db.dumps_pieces(deleted)),
    )
    conn.executemany(
        "INSERT INTO correction_conflicts (document_id, correction_number, target_number)"
        " VALUES (?, ?, ?)",
        [(doc_id, number, t) for t in sorted(conflicts)],
    )
    conn.executemany(
        "INSERT INTO entity_effects (document_id, entity_id, correction_number)"
        " VALUES (?, ?, ?)",
        [(doc_id, ent_id, number) for ent_id in effects],
    )
    db.save_pieces(conn, doc_id, new_pieces)
    revision = _bump_revision(conn, doc_id)
    return {"correction_number": number, "kind": kind, "revision": revision}


def retract_correction(conn: sqlite3.Connection, doc_id: int, number: int) -> dict:
    """按号撤回一笔勘误。

    若仍生效的后续勘误改删了该笔写入的文字、在其中插字，或在该笔纯删除
    留下的接缝插字/跨缝改删，则抛出 ConflictError（携带冲突勘误号），
    当前版保持不变；否则只还原这一笔。
    """
    _get_document(conn, doc_id)
    row = conn.execute(
        "SELECT * FROM corrections WHERE document_id = ? AND number = ?",
        (doc_id, number),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"勘误号不存在: {number}")
    if row["status"] == "retracted":
        raise ValidationError(f"勘误 {number} 已撤回")

    conflicts = db.active_conflicts_against(conn, doc_id, number)
    if conflicts:
        raise ConflictError("存在仍生效的冲突后续勘误，保留现版", conflicts)

    pieces = db.load_pieces(conn, doc_id)
    deleted = db.loads_pieces(row["deleted_pieces"])
    new_pieces = engine.revert_edit(pieces, deleted, number, row["kind"])

    db.save_pieces(conn, doc_id, new_pieces)
    conn.execute(
        "UPDATE corrections SET status = 'retracted'"
        " WHERE document_id = ? AND number = ?",
        (doc_id, number),
    )
    # 该笔不再生效：它对标注的影响随之解除
    conn.execute(
        "DELETE FROM entity_effects WHERE document_id = ? AND correction_number = ?",
        (doc_id, number),
    )
    revision = _bump_revision(conn, doc_id)
    return {"retracted": number, "revision": revision}


# ---------------------------------------------------------------- 查询

def get_version(conn: sqlite3.Connection, doc_id: int) -> dict:
    """从同一版返回正文、有效标注的当前字符区间及待复核标注。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    text = engine.render(pieces, doc["original_text"],
                         db.correction_texts(conn, doc_id))
    valid, pending = [], []
    for ent in _entities(conn, doc_id):
        item = _entity_view(pieces, ent, _effects_map(conn, doc_id))
        (pending if item["affecting_corrections"] else valid).append(item)
    return {
        "document_id": doc_id,
        "revision": doc["revision"],
        "text": text,
        "valid_entities": valid,
        "pending_entities": pending,
    }


def search_entities(conn: sqlite3.Connection, doc_id: int,
                    kind: str | None = None, q: str | None = None) -> list[dict]:
    """实体检索：按类型（人名/地名）和名称子串过滤，返回当前区间与状态。"""
    doc = _get_document(conn, doc_id)
    pieces = db.load_pieces(conn, doc_id)
    effects = _effects_map(conn, doc_id)
    out = []
    for ent in _entities(conn, doc_id):
        if kind and ent["kind"] != kind:
            continue
        if q and q not in ent["name"]:
            continue
        out.append(_entity_view(pieces, ent, effects))
    return out


def list_corrections(conn: sqlite3.Connection, doc_id: int) -> list[dict]:
    _get_document(conn, doc_id)
    rows = conn.execute(
        "SELECT * FROM corrections WHERE document_id = ? ORDER BY number", (doc_id,)
    ).fetchall()
    conflicts = conn.execute(
        "SELECT correction_number, target_number FROM correction_conflicts"
        " WHERE document_id = ? ORDER BY correction_number, target_number",
        (doc_id,),
    ).fetchall()
    cmap: dict[int, list[int]] = {}
    for c in conflicts:
        cmap.setdefault(c["correction_number"], []).append(c["target_number"])
    return [
        {
            "number": r["number"],
            "kind": r["kind"],
            "text": r["text"],
            "range_start": r["range_start"],
            "range_end": r["range_end"],
            "deleted_text": r["deleted_text"],
            "status": r["status"],
            "touches_corrections": cmap.get(r["number"], []),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id, d.revision, d.created_at,
               length(d.original_text) AS original_length,
               (SELECT COUNT(*) FROM entities e WHERE e.document_id = d.id) AS entity_count,
               (SELECT COUNT(*) FROM corrections c
                 WHERE c.document_id = d.id AND c.status = 'active') AS active_corrections
        FROM documents d ORDER BY d.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 内部

def _get_document(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"文档不存在: {doc_id}")
    return row


def _entities(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT id, number, kind, name, start, "end" FROM entities'
        " WHERE document_id = ? ORDER BY number",
        (doc_id,),
    ).fetchall()


def _effects_map(conn: sqlite3.Connection, doc_id: int) -> dict[int, list[int]]:
    rows = conn.execute(
        "SELECT entity_id, correction_number FROM entity_effects"
        " WHERE document_id = ? ORDER BY correction_number",
        (doc_id,),
    ).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append(r["correction_number"])
    return out


def _entity_view(pieces: list[dict], ent: sqlite3.Row,
                 effects: dict[int, list[int]]) -> dict:
    cs, ce = engine.current_range(pieces, ent["start"], ent["end"])
    affecting = effects.get(ent["id"], [])
    return {
        "number": ent["number"],
        "kind": ent["kind"],
        "name": ent["name"],
        "start": cs,
        "end": ce,
        "status": "pending_review" if affecting else "valid",
        "affecting_corrections": affecting,
        "original_start": ent["start"],
        "original_end": ent["end"],
    }


def _bump_revision(conn: sqlite3.Connection, doc_id: int) -> int:
    conn.execute("UPDATE documents SET revision = revision + 1 WHERE id = ?", (doc_id,))
    return conn.execute(
        "SELECT revision FROM documents WHERE id = ?", (doc_id,)).fetchone()["revision"]
