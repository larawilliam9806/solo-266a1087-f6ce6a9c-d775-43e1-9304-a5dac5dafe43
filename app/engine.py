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


def edit_touches_span(s: int, e: int, has_text: bool, ca: int, cb: int) -> bool:
    """判断应用于当前版 [s, e) 的编辑是否触及当前区间 [ca, cb) 的标注。

    改删标注内文字（区间相交）或在其中插字（严格内部插入）视为触及；
    落在标注边界上的插入不算。
    """
    if s < e and s < cb and ca < e:
        return True
    if has_text and s == e and ca < s < cb:
        return True
    return False
