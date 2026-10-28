"""候选词面板解析: 从 OCR 结果里抽出 "1：七里 / 2：起立 ..." 这样的候选条目。
兼容两种 OCR 输出:
  - 一行 "1：无论滚"        -> 直接解析
  - 数字和词被拆成两行      -> 按同一水平行配对
"""
import re

from .lookup import norm_text

DIGITS = "123456789"


class Candidate:
    __slots__ = ("num", "word", "x", "y", "w", "h")

    def __init__(self, num, word, box):
        self.num = num
        self.word = word
        self.x, self.y, self.w, self.h = box

    def __repr__(self):
        return "C%d:%s" % (self.num, self.word)


def _has_cjk(s):
    return any("\u4e00" <= c <= "\u9fff" for c in (s or ""))


def parse_candidates(result, min_len=1):
    """返回 (候选列表, 当前页号, 总页数)"""
    cands = []
    page, pages = None, None
    lone_digits = []      # 只有数字的行
    lone_words = []       # 没有数字但全是中文的行

    for ln in result.get("lines", []):
        raw = ln.get("t", "")
        t = norm_text(raw)
        if not t:
            continue
        m = re.search(r"(\d+)\s*/\s*(\d+)", raw)
        if m and page is None and not _has_cjk(t):
            page, pages = int(m.group(1)), int(m.group(2))
            continue
        if t.isdigit():
            if len(t) == 1 and t in DIGITS:
                lone_digits.append((int(t), ln))
            continue
        if t[0] in DIGITS and _has_cjk(t[1:]) and len(t) - 1 >= min_len:
            cands.append(Candidate(int(t[0]), t[1:], (ln["x"], ln["y"], ln["w"], ln["h"])))
        elif _has_cjk(t) and len(t) >= min_len:
            lone_words.append(ln)

    # 配对: 数字行和同一水平行右侧的中文行
    for num, dln in lone_digits:
        best = None
        for wl in lone_words:
            if abs(wl["y"] - dln["y"]) > max(dln["h"], wl["h"], 8):
                continue
            if wl["x"] <= dln["x"]:
                continue
            if best is None or wl["x"] < best["x"]:
                best = wl
        if best:
            words = norm_text(best.get("t", ""))
            x0 = min(dln["x"], best["x"])
            y0 = min(dln["y"], best["y"])
            x1 = max(dln["x"] + dln["w"], best["x"] + best["w"])
            y1 = max(dln["y"] + dln["h"], best["y"] + best["h"])
            cands.append(Candidate(num, words, (x0, y0, x1 - x0, y1 - y0)))

    cands.sort(key=lambda c: (c.y, c.x))
    return cands, page, pages


def signature(cands):
    """用于判断翻页是否生效"""
    return tuple((c.num, c.word) for c in cands)
