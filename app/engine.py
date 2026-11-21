"""勘误版本引擎（纯 Python，无第三方依赖）。

文档当前版表示为“片段表”（piece table）：一串有序片段，每个片段引用
原始正文的一段、某笔勘误写入的一段，或是纯删除勘误留下的零长接缝标记。

片段结构：{"kind": "orig" | "ins" | "seam", "src": int | None, "a": int, "b": int}
  - orig：原文 [a, b) 区间
  - ins ：编号为 src 的勘误所写入文本的 [a, b) 区间
  - seam：编号为 src 的纯删除勘误留下的接缝（零长，a == b == 0）

基于片段表的不变量（由冲突规则保证）：
  - 某笔勘误仍生效且无任何仍生效的后续勘误与其冲突时，
    它的 ins 片段一定全部存在、连续且中间不夹任何片段；
    纯删除勘误的 seam 片段一定恰好存在一个。
因此撤回时可以直接整体替换。
"""

from __future__ import annotations


class InvariantError(RuntimeError):
    """片段表不变量被破坏（理论上不应发生，说明引擎有 bug）。"""


def piece(kind: str, src: int | None, a: int, b: int) -> dict:
    return {"kind": kind, "src": src, "a": a, "b": b}


def total_length(pieces: list[dict]) -> int:
    return sum(p["b"] - p["a"] for p in pieces)


def apply_edit(pieces: list[dict], s: int, e: int, text: str, new_src: int):
    """把当前版 [s, e) 左闭右开区间替换为 text（s == e 为纯插入，text 为空为纯删除）。

    返回 (新片段表, 被删片段列表, 冲突目标勘误号集合)。
    冲突目标 = 本次改删/插入触及到的、由更早勘误写入的文字或留下的接缝。
    """
    if not (0 <= s <= e):
        raise ValueError("invalid range")
    if s == e and not text:
        raise ValueError("empty edit")

    head: list[dict] = []      # 区间之前的片段（含位于 s 的接缝）
    deleted: list[dict] = []   # 被本笔删掉的片段（撤回时据此还原）
    tail: list[dict] = []      # 区间之后的片段
    conflicts: set[int] = set()
    seams_at_s: set[int] = set()

    pos = 0
    for p in pieces:
        plen = p["b"] - p["a"]
        ps, pe = pos, pos + plen
        if plen == 0:  # 接缝标记（零长）
            if s < ps < e:
                # 跨缝改删：删除区间横跨接缝 -> 与留下接缝的勘误冲突，接缝被吞掉
                deleted.append(p)
                conflicts.add(p["src"])
            elif ps <= s:
                head.append(p)
                if ps == s:
                    seams_at_s.add(p["src"])
            else:
                tail.append(p)
            pos = pe
            continue
        if pe <= s:
            head.append(p)
        elif ps >= e:
            tail.append(p)
        else:
            # 与 [s, e) 相交的片段，必要时在边界处切开
            if ps < s:
                head.append(piece(p["kind"], p["src"], p["a"], p["a"] + (s - ps)))
                if p["kind"] == "ins":
                    conflicts.add(p["src"])  # 删掉了该勘误写入文字的一部分
            if pe > e:
                tail.append(piece(p["kind"], p["src"], p["b"] - (pe - e), p["b"]))
                if p["kind"] == "ins":
                    conflicts.add(p["src"])
            lo, hi = max(ps, s), min(pe, e)
            if lo < hi:
                deleted.append(piece(p["kind"], p["src"],
                                     p["a"] + (lo - ps), p["a"] + (hi - ps)))
                if p["kind"] == "ins":
                    conflicts.add(p["src"])  # 改删了该勘误写入的文字
            elif p["kind"] == "ins":
                # s == e 且严格落在该勘误写入文字的内部 -> 在其中插字
                conflicts.add(p["src"])
        pos = pe

    if text and s == e:
        # 纯插入：落点恰好在某条纯删除接缝上 -> 接缝插字，冲突
        conflicts |= seams_at_s
        # 落点两侧的非零长片段属于同一笔勘误 -> 逻辑上仍在该笔文字内部插字
        # （该笔的片段此前被其他操作切开过，撤回后才会重新相邻）
        prev = next((q for q in reversed(head) if q["b"] > q["a"]), None)
        nxt = next((q for q in tail if q["b"] > q["a"]), None)
        if (prev and nxt and prev["kind"] == "ins" and nxt["kind"] == "ins"
                and prev["src"] == nxt["src"]):
            conflicts.add(prev["src"])

    new_pieces = head
    if text:
        new_pieces.append(piece("ins", new_src, 0, len(text)))
    elif s < e:
        # 纯删除留下接缝标记，供后续冲突检测与撤回定位
        new_pieces.append(piece("seam", new_src, 0, 0))
    new_pieces += tail
    return new_pieces, deleted, conflicts


def revert_edit(pieces: list[dict], deleted: list[dict], src: int, kind: str) -> list[dict]:
    """撤回编号为 src 的勘误：移除它写入的文字/接缝，还原它当时删掉的片段。

    调用前必须确认不存在仍生效的冲突后续勘误，否则不变量不保证成立。
    """
    if kind == "delete":  # 纯删除：把接缝替换回当时删掉的片段
        idxs = [i for i, p in enumerate(pieces)
                if p["kind"] == "seam" and p["src"] == src]
        if len(idxs) != 1:
            raise InvariantError(f"seam of correction {src} not found exactly once")
        i = idxs[0]
        return pieces[:i] + [dict(d) for d in deleted] + pieces[i + 1:]

    # 插入/替换：把该笔写入的连续片段整体替换回当时删掉的片段
    idxs = [i for i, p in enumerate(pieces)
            if p["kind"] == "ins" and p["src"] == src]
    if not idxs or idxs != list(range(idxs[0], idxs[-1] + 1)):
        raise InvariantError(f"inserted pieces of correction {src} not contiguous")
    return pieces[:idxs[0]] + [dict(d) for d in deleted] + pieces[idxs[-1] + 1:]


def render(pieces: list[dict], original: str, corr_texts: dict[int, str]) -> str:
    """由片段表拼出当前版正文。corr_texts: 勘误号 -> 该笔写入的文本。"""
    out = []
    for p in pieces:
        if p["kind"] == "orig":
            out.append(original[p["a"]:p["b"]])
        elif p["kind"] == "ins":
            out.append(corr_texts[p["src"]][p["a"]:p["b"]])
    return "".join(out)


def current_range(pieces: list[dict], a: int, b: int) -> tuple[int, int]:
    """把原文区间 [a, b) 映射为当前版区间（尽力而为）。

    有效标注（未被改删/插入触及）的原文完整且连续，映射精确；
    待复核标注取幸存原文片段在当前的跨度；原文被删光时退化为落点零长区间。
    """
    pos = 0
    start = end = None
    for p in pieces:
        plen = p["b"] - p["a"]
        if p["kind"] == "orig":
            lo, hi = max(a, p["a"]), min(b, p["b"])
            if lo < hi:
                ms = pos + (lo - p["a"])
                me = ms + (hi - lo)
                start = ms if start is None else min(start, ms)
                end = me if end is None else max(end, me)
        pos += plen
    if start is not None:
        return start, end
    f = _fallback_pos(pieces, a)
    return f, f


def _fallback_pos(pieces: list[dict], x: int) -> int:
    """原文偏移 x 被删除时的映射位置：向右贴到最近幸存原文处。"""
    pos = 0
    for p in pieces:
        plen = p["b"] - p["a"]
        if p["kind"] == "orig":
            if p["a"] <= x < p["b"]:
                return pos + (x - p["a"])
            if x < p["a"]:
                return pos
        pos += plen
    return pos


def _deleted_orig_span(deleted: list[dict] | None) -> tuple[int | None, int | None]:
    """一笔勘误删掉的原文片段覆盖的最小/最大原文下标（无原文则返回 None）。"""
    if not deleted:
        return None, None
    orig = [d for d in deleted if d["kind"] == "orig"]
    if not orig:
        return None, None
    return min(d["a"] for d in orig), max(d["b"] for d in orig)


def insertion_gap(pieces: list[dict], pos: int,
                  deleted_by_src: dict[int, list[dict]] | None = None
                  ) -> tuple[int, bool]:
    """当前版纯插入位置 pos 对应的“原文坐标缝隙”。

    把所有勘误撤回后，插入文字会落回原文哪个缝隙：返回 (gap, soft)，
    gap 为该缝隙前的原文字符下标（第 x 与第 x+1 个字符之间的缝，
    x=0 为文首，x=len(原文) 为文末）；soft 表示该锚点是经更早勘误
    删除/替换掉的原文区间还原出来的，而非幸存原文上的真实缝隙。

    解析顺序：
    1. pos 落在某个幸存原文片段上（含其左右边界）：锚点即片段内对应
       原文下标，soft=False。因此在“替换片段与幸存原文相邻”的边界上
       插字，锚点取幸存原文一侧（如替换 [0,1) 后在其后插字，锚点为 1
       而非 0），仍按幸存原文的边界缝处理（不计入标注触及）；
    2. 否则向两侧查找：替换/删除勘误的片段凭其 deleted_pieces
       （deleted_by_src：勘误号 -> 撤回用片段）还原它替换/删除的原文
       下标，纯插入勘误没有原文痕迹则跳过继续向外；由此得到的锚点
       soft=True；纯删除接缝恰在落点时直接取其原文缝；
    3. 都找不到时退回左侧最近原文缝（soft=False）。

    严格落在其他勘误文字内部的插入必与该勘误冲突、必先于它撤回，因此
    那些观察不到的歧义锚点不影响待复核判定。
    """
    # 落点恰在某条纯删除接缝上：接缝是精确锚点，优先于相邻原文的边界缝
    cur = 0
    for p in pieces:
        plen = p["b"] - p["a"]
        if p["kind"] == "seam" and cur == pos and deleted_by_src is not None:
            lo, _ = _deleted_orig_span(deleted_by_src.get(p["src"]))
            if lo is not None:
                return lo, True
        cur += plen

    cur = 0
    for p in pieces:
        plen = p["b"] - p["a"]
        # 零长接缝本身可能恰在 pos：不能按相邻原文片段的边界提前命中，
        # 已在上方接缝预扫中处理
        if (p["kind"] == "orig" and plen > 0 and cur <= pos <= cur + plen):
            return p["a"] + (pos - cur), False
        cur += plen

    cur = 0
    left_gap = 0
    right_anchor = None
    for p in pieces:
        plen = p["b"] - p["a"]
        ps, pe = cur, cur + plen
        if p["kind"] == "orig":
            if pe <= pos:
                left_gap = p["b"]            # 最近的左侧原文片段
            elif ps >= pos and right_anchor is None:
                right_anchor = p["a"]        # 最近的右侧原文片段
        elif deleted_by_src is not None and (pe <= pos or ps >= pos):
            lo, hi = _deleted_orig_span(deleted_by_src.get(p["src"]))
            if lo is not None:
                if pe <= pos:
                    left_gap = hi             # 按顺序后者更近，直接覆盖
                elif right_anchor is None:
                    right_anchor = lo
        cur = pe

    if right_anchor is not None:
        return right_anchor, True
    return left_gap, False


def edit_touches_original(deleted: list[dict], is_insert: bool,
                         gap: int, soft: bool, a: int, b: int) -> bool:
    """判断一笔编辑是否触及原文区间 [a, b) 的标注。

    两条规则（与原文字段当前是否已被更早的勘误替换/删除无关）：
    1. 改删：本笔删掉的片段中含落在 [a, b) 内的原文片段；
    2. 纯插入：落点的原文坐标缝隙位于标注内部。幸存原文上严格内部
       （a < gap < b）才算，边界缝不算；若锚点来自被删除/替换的原文
       区间（soft），则其边界缝也位于标注曾经占据的位置，计入触及。
    """
    if not is_insert:
        for d in deleted:
            if d["kind"] == "orig" and d["a"] < b and a < d["b"]:
                return True
        return False
    if a < gap < b:
        return True
    return soft and a <= gap <= b
