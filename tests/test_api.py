"""API 集成测试：FastAPI TestClient + 临时 SQLite 库。

运行：python3 -m pytest tests/test_api.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def import_doc(client, text="张三住在北京市东城区。", entities=None):
    if entities is None:
        entities = [
            {"number": 1, "kind": "person", "name": "张三", "start": 0, "end": 2},
            {"number": 2, "kind": "place", "name": "北京市", "start": 4, "end": 7},
        ]
    payload = {"text": text, "entities": entities}
    r = client.post("/documents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["document_id"]


def correct(client, doc, start, end, text=""):
    r = client.post(f"/documents/{doc}/corrections",
                    json={"start": start, "end": end, "text": text})
    assert r.status_code == 201, r.text
    return r.json()["correction_number"]


def retract(client, doc, number):
    return client.post(f"/documents/{doc}/corrections/{number}/retract")


def version(client, doc):
    r = client.get(f"/documents/{doc}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 导入与查询

def test_import_and_version_roundtrip(client):
    doc = import_doc(client)
    v = version(client, doc)
    assert v["text"] == "张三住在北京市东城区。"
    assert v["revision"] == 0
    assert v["pending_entities"] == []
    assert [(e["number"], e["start"], e["end"]) for e in v["valid_entities"]] == [
        (1, 0, 2), (2, 4, 7)]


def test_import_entity_number_autofill_and_validation(client):
    r = client.post("/documents", json={"text": "北京市。", "entities": [
        {"kind": "place", "name": "北京市", "start": 0, "end": 3}]})
    assert r.status_code == 201
    assert client.get(f"/documents/{r.json()['document_id']}/entities"
                      ).json()[0]["number"] == 1
    # 区间越界
    r = client.post("/documents", json={"text": "短", "entities": [
        {"kind": "place", "name": "x", "start": 0, "end": 9}]})
    assert r.status_code == 400
    # 编号重复
    r = client.post("/documents", json={"text": "北京市。", "entities": [
        {"number": 1, "kind": "place", "name": "北京", "start": 0, "end": 2},
        {"number": 1, "kind": "place", "name": "市", "start": 2, "end": 3}]})
    assert r.status_code == 400


# ---------------------------------------------------------------- 勘误与撤回

def test_correction_lifecycle(client):
    doc = import_doc(client, "ABCDEFG")
    n = correct(client, doc, 2, 4, "XY")     # ABXYEFG
    assert n == 1
    assert version(client, doc)["text"] == "ABXYEFG"
    r = retract(client, doc, n)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCDEFG"


def test_archivist_retracts_wrong_correction_keeps_later_fix(client):
    """档案员场景：撤回改错的一笔，保留后来改对的文字。"""
    doc = import_doc(client, "张三住在胡同里。")
    # 勘误 1（改错了）：胡同 -> 海滩
    correct(client, doc, 4, 6, "海滩")
    assert version(client, doc)["text"] == "张三住在海滩里。"
    # 勘误 2（后来改对别处）：补“曾”字
    correct(client, doc, 2, 2, "曾")
    assert version(client, doc)["text"] == "张三曾住在海滩里。"
    # 撤回勘误 1：无冲突，只还原这一笔，勘误 2 的成果保留
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "张三曾住在胡同里。"


def test_conflict_when_later_correction_modifies_written_text(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1: ABCXYZDEFG
    correct(client, doc, 4, 5, "q")          # 2: 改掉了 1 写入的 Y
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]
    assert version(client, doc)["text"] == "ABCXqZDEFG"   # 保留现版
    assert retract(client, doc, 2).status_code == 200
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCDEFG"


def test_conflict_when_insert_within_written_text(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1
    correct(client, doc, 4, 4, "q")          # 2: 在 1 写入的文字中插字
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]


def test_no_conflict_at_boundary_of_written_text(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1: ABCXYZDEFG
    correct(client, doc, 3, 3, "q")          # 2: 边界插入，不算“在其中”
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCqDEFG"


def test_pure_delete_seam_insert_conflicts(client):
    doc = import_doc(client, "ABCDEFGHIJ")
    correct(client, doc, 2, 5)               # 1: 纯删除 CDE -> ABFGHIJ
    correct(client, doc, 2, 2, "Z")          # 2: 恰在接缝上插字
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]
    assert retract(client, doc, 2).status_code == 200
    assert retract(client, doc, 1).status_code == 200
    assert version(client, doc)["text"] == "ABCDEFGHIJ"


def test_pure_delete_cross_seam_edit_conflicts(client):
    doc = import_doc(client, "ABCDEFGHIJ")
    correct(client, doc, 2, 5)               # 1: 删 CDE，接缝在 2
    correct(client, doc, 1, 4)               # 2: 跨缝改删 BFG
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2]
    assert version(client, doc)["text"] == "AHIJ"           # 保留现版


def test_pure_delete_one_side_edit_no_conflict(client):
    doc = import_doc(client, "ABCDEFGHIJ")
    correct(client, doc, 2, 5)               # 1: 删 CDE
    correct(client, doc, 2, 4)               # 2: 只删缝后的 FG
    r = retract(client, doc, 1)
    assert r.status_code == 200
    assert version(client, doc)["text"] == "ABCDEHIJ"
    assert retract(client, doc, 2).status_code == 200
    assert version(client, doc)["text"] == "ABCDEFGHIJ"


def test_multiple_conflicts_reported(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")        # 1
    correct(client, doc, 4, 4, "p")          # 2: 在 XYZ 中插字
    correct(client, doc, 5, 6, "q")          # 3: 改掉 XYZ 的 Y
    r = retract(client, doc, 1)
    assert r.status_code == 409
    assert r.json()["conflicts"] == [2, 3]


def test_correction_validation(client):
    doc = import_doc(client, "ABC", entities=[])
    assert client.post(f"/documents/{doc}/corrections",
                       json={"start": 0, "end": 9, "text": "x"}).status_code == 400
    assert client.post(f"/documents/{doc}/corrections",
                       json={"start": 1, "end": 1, "text": ""}).status_code == 400
    assert retract(client, doc, 99).status_code == 404
    correct(client, doc, 0, 1, "x")
    assert retract(client, doc, 1).status_code == 200
    assert retract(client, doc, 1).status_code == 400   # 重复撤回
    assert client.post("/documents/999/corrections",
                       json={"start": 0, "end": 0, "text": "x"}).status_code == 404


def test_revision_increases_and_is_consistent(client):
    doc = import_doc(client)
    correct(client, doc, 0, 0, "旁白：")
    v = version(client, doc)
    assert v["revision"] == 1
    assert v["text"].startswith("旁白：")
    retract(client, doc, 1)
    assert version(client, doc)["revision"] == 2


# ---------------------------------------------------------------- 标注待复核

def test_entity_pending_on_inner_edit_and_recovers(client):
    doc = import_doc(client)
    correct(client, doc, 5, 6, "津")         # 改“北京市”内的“京”
    v = version(client, doc)
    assert [e["number"] for e in v["valid_entities"]] == [1]
    assert len(v["pending_entities"]) == 1
    pend = v["pending_entities"][0]
    assert pend["number"] == 2 and pend["status"] == "pending_review"
    assert pend["affecting_corrections"] == [1]
    retract(client, doc, 1)
    v = version(client, doc)
    assert v["pending_entities"] == []
    assert [(e["number"], e["start"], e["end"]) for e in v["valid_entities"]] == [
        (1, 0, 2), (2, 4, 7)]


def test_entity_pending_until_all_affecting_retracted(client):
    doc = import_doc(client)
    correct(client, doc, 5, 6, "津")         # 1: 触及标注 2
    correct(client, doc, 6, 7, "城")         # 2: 也触及标注 2
    assert retract(client, doc, 1).status_code == 200
    pend = version(client, doc)["pending_entities"]
    assert len(pend) == 1 and pend[0]["affecting_corrections"] == [2]
    assert retract(client, doc, 2).status_code == 200
    assert version(client, doc)["pending_entities"] == []


def test_entity_stays_pending_after_earlier_replace_retracted(client):
    """连续勘误撤回：先替换标注首字，再在替换片段与幸存原文之间插字。

    撤回替换笔后正文恢复原字，但插入笔仍影响原标注，实体必须保持待复核，
    直到插入笔也撤回；恢复有效后区间按当前正文返回。
    """
    doc = import_doc(client, "张三", entities=[
        {"number": 1, "kind": "person", "name": "张三", "start": 0, "end": 2}])
    correct(client, doc, 0, 1, "李")         # 1: 张 -> 李（触及原标注 [0,2)）
    correct(client, doc, 1, 1, "小")         # 2: 当前稿“李|三”之间插“小”
    v = version(client, doc)
    assert v["text"] == "李小三"
    assert v["valid_entities"] == []
    assert v["pending_entities"][0]["affecting_corrections"] == [1, 2]
    # 撤回第一笔：正文为“张小三”，但第二笔仍计为影响
    assert retract(client, doc, 1).status_code == 200
    v = version(client, doc)
    assert v["text"] == "张小三"
    assert v["valid_entities"] == []
    pend = v["pending_entities"]
    assert len(pend) == 1 and pend[0]["affecting_corrections"] == [2]
    assert (pend[0]["start"], pend[0]["end"]) == (0, 3)   # 按当前正文的区间
    # 第二笔也撤回后才恢复有效，区间与当前正文一致
    assert retract(client, doc, 2).status_code == 200
    v = version(client, doc)
    assert v["text"] == "张三"
    assert v["pending_entities"] == []
    assert [(e["number"], e["start"], e["end"])
            for e in v["valid_entities"]] == [(1, 0, 2)]


def test_insert_between_two_replaces_keeps_effect_after_retractions(client):
    """两笔替换分别改掉标注内两字，中间插入一笔：逐笔撤回替换不丢插入影响。"""
    doc = import_doc(client, "ABCD", entities=[
        {"number": 1, "kind": "person", "name": "AB", "start": 0, "end": 2},
        {"number": 2, "kind": "place", "name": "CD", "start": 2, "end": 4}])
    correct(client, doc, 0, 1, "x")          # 1: A -> x，触及实体 1
    correct(client, doc, 1, 2, "y")          # 2: B -> y，触及实体 1
    correct(client, doc, 1, 1, "中")         # 3: 在 x|y 之间插字，锚点缝 1 在 [0,2) 内
    v = version(client, doc)
    assert v["text"] == "x中yCD"
    assert {e["number"] for e in v["pending_entities"]} == {1}
    retract(client, doc, 1)
    v = version(client, doc)
    assert v["text"] == "A中yCD"
    assert {e["number"]: e["affecting_corrections"]
            for e in v["pending_entities"]} == {1: [2, 3]}
    retract(client, doc, 2)
    v = version(client, doc)
    assert v["text"] == "A中BCD"
    pend = {e["number"]: e for e in v["pending_entities"]}
    assert set(pend) == {1}
    assert pend[1]["affecting_corrections"] == [3]
    assert (pend[1]["start"], pend[1]["end"]) == (0, 3)
    retract(client, doc, 3)
    v = version(client, doc)
    assert v["text"] == "ABCD"
    assert v["pending_entities"] == []


def test_unrelated_entity_untouched_by_replacement_chain(client):
    """无关标注沿用原规则：勘误只触及实体 1 时，实体 2 始终有效且区间平移。"""
    doc = import_doc(client)
    correct(client, doc, 0, 1, "李")
    correct(client, doc, 1, 1, "小")
    retract(client, doc, 1)
    v = version(client, doc)
    place = [e for e in v["valid_entities"] if e["number"] == 2][0]
    assert place["status"] == "valid"
    assert (place["start"], place["end"]) == (5, 8)   # 前插一字，整体后移


def test_entity_pending_on_insert_within_and_delete(client):
    doc = import_doc(client)
    correct(client, doc, 5, 5, "（音）")      # 在“北京市”中插字
    assert len(version(client, doc)["pending_entities"]) == 1
    retract(client, doc, 1)
    correct(client, doc, 4, 7)               # 删掉整个“北京市”
    v = version(client, doc)
    assert len(v["pending_entities"]) == 1
    assert v["text"] == "张三住在东城区。"


def test_entity_boundary_insert_does_not_pending(client):
    doc = import_doc(client)
    correct(client, doc, 4, 4, "曾")          # 标注起点处插入：不算“其中”
    v = version(client, doc)
    assert v["pending_entities"] == []
    place = [e for e in v["valid_entities"] if e["number"] == 2][0]
    assert (place["start"], place["end"]) == (5, 8)   # 区间随版本平移


def test_entity_range_shifts_with_distant_edits(client):
    doc = import_doc(client)
    correct(client, doc, 0, 0, "旁白：")      # 文首插入 3 字
    v = version(client, doc)
    rng = {e["number"]: (e["start"], e["end"]) for e in v["valid_entities"]}
    assert rng[1] == (3, 5)
    assert rng[2] == (7, 10)


# ---------------------------------------------------------------- 实体检索

def test_entity_search_filters(client):
    doc = import_doc(client)
    r = client.get(f"/documents/{doc}/entities", params={"kind": "person"})
    assert [e["name"] for e in r.json()] == ["张三"]
    r = client.get(f"/documents/{doc}/entities", params={"q": "北京"})
    assert [e["number"] for e in r.json()] == [2]
    r = client.get(f"/documents/{doc}/entities/2")
    assert r.json()["name"] == "北京市"
    assert client.get(f"/documents/{doc}/entities/99").status_code == 404


def test_entity_search_reflects_pending_status(client):
    doc = import_doc(client)
    correct(client, doc, 5, 6, "津")
    ent = client.get(f"/documents/{doc}/entities/2").json()
    assert ent["status"] == "pending_review"
    assert ent["affecting_corrections"] == [1]


# ---------------------------------------------------------------- 勘误流水

def test_corrections_listing(client):
    doc = import_doc(client, "ABCDEFG")
    correct(client, doc, 3, 3, "XYZ")
    correct(client, doc, 4, 5, "q")
    rows = client.get(f"/documents/{doc}/corrections").json()
    assert [r["number"] for r in rows] == [1, 2]
    assert rows[0]["kind"] == "insert" and rows[0]["status"] == "active"
    assert rows[1]["touches_corrections"] == [1]
    assert rows[1]["deleted_text"] == "Y"
