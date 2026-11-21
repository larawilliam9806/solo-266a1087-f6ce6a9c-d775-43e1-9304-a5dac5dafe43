"""引擎单元测试：仅标准库，可直接 `python3 -m unittest` 运行。"""

import unittest

from app import engine


def render(pieces, original="", corr_texts=None):
    return engine.render(pieces, original, corr_texts or {})


class ApplyEditTest(unittest.TestCase):
    def test_insert_into_original(self):
        pieces = [engine.piece("orig", None, 0, 6)]  # ABCDEF
        new, deleted, conflicts = engine.apply_edit(pieces, 3, 3, "XY", 1)
        self.assertEqual(render(new, "ABCDEF", {1: "XY"}), "ABCXYDEF")
        self.assertEqual(deleted, [])
        self.assertEqual(conflicts, set())

    def test_delete_from_original_leaves_seam(self):
        pieces = [engine.piece("orig", None, 0, 6)]
        new, deleted, conflicts = engine.apply_edit(pieces, 2, 4, "", 1)
        self.assertEqual(render(new, "ABCDEF"), "ABEF")
        self.assertEqual([d["kind"] for d in deleted], ["orig"])
        self.assertEqual(conflicts, set())
        self.assertTrue(any(p["kind"] == "seam" and p["src"] == 1 for p in new))

    def test_replace(self):
        pieces = [engine.piece("orig", None, 0, 6)]
        new, deleted, conflicts = engine.apply_edit(pieces, 2, 4, "XYZ", 1)
        self.assertEqual(render(new, "ABCDEF", {1: "XYZ"}), "ABXYZEF")
        self.assertEqual(render(deleted, "ABCDEF"), "CD")

    def test_delete_inserted_text_conflicts(self):
        pieces = [engine.piece("orig", None, 0, 3),
                  engine.piece("ins", 1, 0, 3),
                  engine.piece("orig", None, 3, 6)]  # ABC + XYZ + DEF
        # 删掉 Y（勘误 1 写入文字的一部分）
        new, deleted, conflicts = engine.apply_edit(pieces, 4, 5, "", 2)
        self.assertEqual(conflicts, {1})
        self.assertEqual(render(new, "ABCDEF", {1: "XYZ"}), "ABCXZDEF")

    def test_insert_within_inserted_text_conflicts(self):
        pieces = [engine.piece("orig", None, 0, 3),
                  engine.piece("ins", 1, 0, 3),
                  engine.piece("orig", None, 3, 6)]
        new, deleted, conflicts = engine.apply_edit(pieces, 4, 4, "q", 2)
        self.assertEqual(conflicts, {1})
        self.assertEqual(render(new, "ABCDEF", {1: "XYZ", 2: "q"}), "ABCXqYZDEF")

    def test_insert_at_boundary_of_inserted_text_no_conflict(self):
        pieces = [engine.piece("orig", None, 0, 3),
                  engine.piece("ins", 1, 0, 3),
                  engine.piece("orig", None, 3, 6)]
        for at in (3, 6):  # 写在文字的起点/终点边界都不算“在其中”
            _, _, conflicts = engine.apply_edit(pieces, at, at, "q", 2)
            self.assertEqual(conflicts, set(), f"at={at}")

    def test_insert_at_seam_conflicts(self):
        # 纯删除 1 留下接缝，随后恰好插在接缝上
        pieces = [engine.piece("orig", None, 0, 2),
                  engine.piece("seam", 1, 0, 0),
                  engine.piece("orig", None, 5, 8)]
        _, _, conflicts = engine.apply_edit(pieces, 2, 2, "Z", 2)
        self.assertEqual(conflicts, {1})

    def test_cross_seam_delete_conflicts(self):
        pieces = [engine.piece("orig", None, 0, 2),
                  engine.piece("seam", 1, 0, 0),
                  engine.piece("orig", None, 5, 8)]  # AB + FGH
        _, deleted, conflicts = engine.apply_edit(pieces, 1, 4, "", 2)
        self.assertEqual(conflicts, {1})
        self.assertIn("seam", [d["kind"] for d in deleted])

    def test_one_side_of_seam_delete_no_conflict(self):
        pieces = [engine.piece("orig", None, 0, 2),
                  engine.piece("seam", 1, 0, 0),
                  engine.piece("orig", None, 5, 8)]
        _, _, conflicts = engine.apply_edit(pieces, 2, 4, "", 2)  # 只删缝后文字
        self.assertEqual(conflicts, set())
        _, _, conflicts = engine.apply_edit(pieces, 0, 2, "", 2)  # 只删缝前文字
        self.assertEqual(conflicts, set())

    def test_insert_between_split_pieces_of_same_correction_conflicts(self):
        # 勘误 1 的 ins 片段被切开后，在切口处插入仍算“在其中插字”
        pieces = [engine.piece("orig", None, 0, 2),
                  engine.piece("ins", 1, 0, 1),
                  engine.piece("ins", 1, 1, 3),
                  engine.piece("orig", None, 2, 4)]
        _, _, conflicts = engine.apply_edit(pieces, 3, 3, "W", 3)
        self.assertEqual(conflicts, {1})


class RevertEditTest(unittest.TestCase):
    def test_revert_insert(self):
        pieces = [engine.piece("orig", None, 0, 6)]
        pieces, deleted, _ = engine.apply_edit(pieces, 3, 3, "XY", 1)
        pieces = engine.revert_edit(pieces, deleted, 1, "insert")
        self.assertEqual(render(pieces, "ABCDEF"), "ABCDEF")

    def test_revert_delete_restores_text(self):
        pieces = [engine.piece("orig", None, 0, 10)]  # ABCDEFGHIJ
        pieces, d1, _ = engine.apply_edit(pieces, 2, 5, "", 1)   # ABFGHIJ
        pieces, d2, _ = engine.apply_edit(pieces, 2, 4, "", 2)   # ABHIJ
        # 撤回 1：恢复 CDE，勘误 2 的删除保持有效
        pieces = engine.revert_edit(pieces, d1, 1, "delete")
        self.assertEqual(render(pieces, "ABCDEFGHIJ"), "ABCDEHIJ")
        # 再撤回 2：恢复 FG
        pieces = engine.revert_edit(pieces, d2, 2, "delete")
        self.assertEqual(render(pieces, "ABCDEFGHIJ"), "ABCDEFGHIJ")

    def test_revert_replace_keeps_later_boundary_insert(self):
        pieces = [engine.piece("orig", None, 0, 5)]  # ABCDE
        pieces, d1, _ = engine.apply_edit(pieces, 1, 3, "XY", 1)  # AXYDE
        pieces, d2, _ = engine.apply_edit(pieces, 1, 1, "q", 2)   # AqXYDE（边界插入，不冲突）
        pieces = engine.revert_edit(pieces, d1, 1, "replace")     # 只还原勘误 1
        self.assertEqual(render(pieces, "ABCDE", {2: "q"}), "AqBCDE")

    def test_revert_after_conflicting_edit_retracted(self):
        # 1 写入 XYZ，2 在其中插 q（冲突）；先撤回 2，再撤回 1 应成立
        pieces = [engine.piece("orig", None, 0, 6)]  # ABCDEF
        pieces, d1, _ = engine.apply_edit(pieces, 3, 3, "XYZ", 1)
        pieces, d2, c2 = engine.apply_edit(pieces, 4, 4, "q", 2)
        self.assertEqual(c2, {1})
        pieces = engine.revert_edit(pieces, d2, 2, "insert")
        pieces = engine.revert_edit(pieces, d1, 1, "insert")
        self.assertEqual(render(pieces, "ABCDEF"), "ABCDEF")

    def test_revert_replace_with_later_adjacent_seam(self):
        # 1 替换 BC->XY；2 纯删除后面的 E（与 1 不冲突，留下相邻接缝）
        pieces = [engine.piece("orig", None, 0, 6)]  # ABCDEF
        pieces, d1, _ = engine.apply_edit(pieces, 1, 3, "XY", 1)   # AXYDEF
        pieces, d2, c2 = engine.apply_edit(pieces, 4, 5, "", 2)    # AXYDF
        self.assertEqual(c2, set())
        self.assertEqual(render(pieces, "ABCDEF", {1: "XY"}), "AXYDF")
        pieces = engine.revert_edit(pieces, d1, 1, "replace")      # 只还原 1
        self.assertEqual(render(pieces, "ABCDEF"), "ABCDF")        # 2 的删除保留
        pieces = engine.revert_edit(pieces, d2, 2, "delete")
        self.assertEqual(render(pieces, "ABCDEF"), "ABCDEF")


class RangeMappingTest(unittest.TestCase):
    def test_current_range_shifts_with_edits(self):
        pieces = [engine.piece("orig", None, 0, 10)]  # ABCDEFGHIJ
        self.assertEqual(engine.current_range(pieces, 4, 7), (4, 7))
        pieces, _, _ = engine.apply_edit(pieces, 2, 2, "XYZ", 1)  # 前面插入 3 字
        self.assertEqual(engine.current_range(pieces, 4, 7), (7, 10))
        pieces, _, _ = engine.apply_edit(pieces, 0, 1, "", 2)     # 前面删 1 字
        self.assertEqual(engine.current_range(pieces, 4, 7), (6, 9))

    def test_current_range_partially_deleted(self):
        pieces = [engine.piece("orig", None, 0, 10)]
        pieces, _, _ = engine.apply_edit(pieces, 5, 8, "", 1)  # 删 FGH
        # 标注 [4,7)：E 幸存、FG 被删 -> 跨度退化为幸存部分
        self.assertEqual(engine.current_range(pieces, 4, 7), (4, 5))

    def test_current_range_fully_deleted_collapses(self):
        pieces = [engine.piece("orig", None, 0, 10)]
        pieces, _, _ = engine.apply_edit(pieces, 4, 7, "", 1)
        start, end = engine.current_range(pieces, 4, 7)
        self.assertEqual(start, end)


class TouchSpanTest(unittest.TestCase):
    def test_rules(self):
        T = engine.edit_touches_span
        self.assertTrue(T(3, 5, False, 4, 8))     # 删除与标注相交
        self.assertTrue(T(4, 8, False, 4, 8))     # 删除整个标注
        self.assertFalse(T(1, 4, False, 4, 8))    # 删除止于标注起点
        self.assertFalse(T(8, 9, False, 4, 8))    # 删除起于标注终点
        self.assertTrue(T(6, 6, True, 4, 8))      # 标注内部插入
        self.assertTrue(T(4, 6, True, 4, 8))      # 替换删掉标注内文字
        self.assertFalse(T(8, 9, True, 4, 8))     # 替换区间在标注外
        self.assertFalse(T(4, 4, True, 4, 8))     # 边界插入不算“其中”
        self.assertFalse(T(8, 8, True, 4, 8))


if __name__ == "__main__":
    unittest.main()
