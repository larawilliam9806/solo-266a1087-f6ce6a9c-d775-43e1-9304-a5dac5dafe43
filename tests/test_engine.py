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


class InsertionGapTest(unittest.TestCase):
    def test_gap_on_plain_original(self):
        pieces = [engine.piece("orig", None, 0, 6)]  # ABCDEF
        self.assertEqual(engine.insertion_gap(pieces, 0), (0, False))
        self.assertEqual(engine.insertion_gap(pieces, 3), (3, False))
        self.assertEqual(engine.insertion_gap(pieces, 6), (6, False))

    def test_gap_beside_replacement_prefers_surviving_original(self):
        # 替换 [0,1) 的 A -> X，当前为 X|BCDEF：在替换片段与幸存原文之间插字
        pieces = [engine.piece("ins", 1, 0, 1),
                  engine.piece("orig", None, 1, 6)]
        deleted = {1: [engine.piece("orig", None, 0, 1)]}
        # 锚点取幸存原文一侧且非 soft：撤回后插入落在原文缝 1，按边界缝处理
        self.assertEqual(engine.insertion_gap(pieces, 1, deleted), (1, False))

    def test_gap_between_two_replacements_resolves_via_deleted_spans(self):
        # A->X（替换 [0,1)）、B->Y（替换 [1,2)），当前 X|Y|CDEF，中间插字
        pieces = [engine.piece("ins", 1, 0, 1),
                  engine.piece("ins", 2, 0, 1),
                  engine.piece("orig", None, 2, 6)]
        deleted = {1: [engine.piece("orig", None, 0, 1)],
                   2: [engine.piece("orig", None, 1, 2)]}
        # 两侧都无幸存原文，锚点来自被替换的原文区间，soft=True
        self.assertEqual(engine.insertion_gap(pieces, 1, deleted), (1, True))

    def test_gap_at_pure_delete_seam(self):
        # 纯删除 [2,5) 的 CDE 留下接缝，当前 AB|FGH，恰在接缝上插字
        pieces = [engine.piece("orig", None, 0, 2),
                  engine.piece("seam", 1, 0, 0),
                  engine.piece("orig", None, 5, 8)]
        deleted = {1: [engine.piece("orig", None, 2, 5)]}
        self.assertEqual(engine.insertion_gap(pieces, 2, deleted), (2, True))

    def test_gap_without_anchor_map_falls_back_to_left(self):
        pieces = [engine.piece("ins", 1, 0, 1),
                  engine.piece("orig", None, 1, 6)]
        # 无 deleted_by_src 时仍能靠幸存原文定位（右侧锚点优先，非 soft）
        self.assertEqual(engine.insertion_gap(pieces, 1), (1, False))
        # 两侧都是 ins 且无映射时退回左侧最近原文缝
        both_ins = [engine.piece("ins", 1, 0, 1), engine.piece("ins", 2, 0, 1)]
        self.assertEqual(engine.insertion_gap(both_ins, 1), (0, False))

    def test_gap_beside_pure_insert_correction_skips_it(self):
        # 文首纯插入（无原文痕迹），随后才是原文：插入点在二者边界
        pieces = [engine.piece("ins", 1, 0, 2),
                  engine.piece("orig", None, 0, 4)]
        self.assertEqual(engine.insertion_gap(pieces, 2, {}), (0, False))


class EditTouchesOriginalTest(unittest.TestCase):
    def test_replacement_deleting_original_inside_span(self):
        deleted = [engine.piece("orig", None, 0, 1)]
        self.assertTrue(
            engine.edit_touches_original(deleted, False, None, False, 0, 2))
        self.assertFalse(
            engine.edit_touches_original(deleted, False, None, False, 2, 4))

    def test_pure_insert_uses_strict_inner_gap_on_surviving_text(self):
        T = engine.edit_touches_original
        self.assertTrue(T([], True, 1, False, 0, 2))    # 缝 1 在标注内部
        self.assertFalse(T([], True, 0, False, 0, 2))   # 幸存原文边界缝不算
        self.assertFalse(T([], True, 2, False, 0, 2))
        self.assertFalse(T([], True, 5, False, 0, 2))

    def test_pure_insert_at_soft_boundary_gap_touches(self):
        T = engine.edit_touches_original
        # 锚点来自被删/替换原文（接缝等）：标注边界缝也算触及
        self.assertTrue(T([], True, 0, True, 0, 2))
        self.assertTrue(T([], True, 2, True, 0, 2))
        # soft 锚点在标注外仍不算
        self.assertFalse(T([], True, 5, True, 0, 2))

    def test_editing_only_inserted_text_does_not_touch_original(self):
        # 本笔删掉的只是更早勘误写入的文字，没有原文片段 -> 不按原文触及
        deleted = [engine.piece("ins", 1, 0, 1)]
        self.assertFalse(
            engine.edit_touches_original(deleted, False, None, False, 0, 2))


if __name__ == "__main__":
    unittest.main()
