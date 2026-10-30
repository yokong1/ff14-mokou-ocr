"""词库: 词条 -> 拼音, OCR 文本纠错, 多音字变体生成。"""
import difflib
import json
import os
import re
from collections import defaultdict

CJK = re.compile(r"[\u4e00-\u9fff]")
FULLWIDTH = {ord(c): ord(c) - 0xFEE0 for c in
             "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
             "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ：（）"}


def norm_text(s):
    """去掉空格/标点, 全角转半角, 只保留中文与字母数字。"""
    if not s:
        return ""
    s = s.translate(FULLWIDTH)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    return s


def norm_loose(s):
    """只去空白, 保留字母, 用于判断输入框里有没有残留内容"""
    if not s:
        return ""
    s = s.translate(FULLWIDTH)
    return re.sub(r"[\s\u3000]+", "", s)


def has_latin(s):
    """含拉丁字母/数字 -> 多半是输入框里的拼音, 不是怪名"""
    return bool(re.search(r"[A-Za-z0-9]", s or ""))


def plain_pinyin(py):
    return re.sub(r"[^a-z]", "", (py or "").lower())


class Match:
    __slots__ = ("word", "pinyin", "score", "exact")

    def __init__(self, word, pinyin, score, exact):
        self.word, self.pinyin, self.score, self.exact = word, pinyin, score, exact

    def __repr__(self):
        return "Match(%s->%s %.2f%s)" % (self.word, plain_pinyin(self.pinyin), self.score,
                                         " exact" if self.exact else "")


class Lexicon:
    def __init__(self, tsv_path, learned_path=None):
        self.path = tsv_path
        self.learned_path = learned_path
        self.words = {}                 # 词 -> "ke lao de"
        self.char_readings = defaultdict(list)
        self.learned = {}               # 词 -> 确认过的拼音(带空格)
        self.corrections = {}           # OCR 原样文本 -> 正确词条
        self._load()
        if learned_path and os.path.exists(learned_path):
            try:
                data = json.load(open(learned_path, encoding="utf-8"))
                self.learned = data.get("pinyin", {})
                self.corrections = data.get("corrections", {})
            except Exception:
                pass
        for w, p in self.learned.items():
            self.words[w] = p
        self._char_freq = defaultdict(lambda: defaultdict(int))
        for w, p in self.words.items():
            syl = p.split()
            if len(syl) == len(w):
                for ch, s in zip(w, syl):
                    self._char_freq[ch][s] += 1

    # ---------- 载入 ----------
    def _load(self):
        with open(self.path, "rb") as stream:
            raw = stream.read()
        text = None
        for enc in ("utf-8-sig", "gbk"):
            try:
                t = raw.decode(enc)
            except UnicodeDecodeError:
                continue
            if "词条" in t[:60]:
                text = t
                break
            text = text or t
        if text is None:
            text = raw.decode("utf-8", "replace")

        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            word = norm_text(parts[0])
            syl = [re.sub(r"[^a-z]", "", s.lower()) for s in parts[1].split()]
            syl = [s for s in syl if s]
            if not word or not syl or not CJK.search(word):
                continue
            self.words.setdefault(word, " ".join(syl))
            if len(syl) == len(word):
                for ch, s in zip(word, syl):
                    if s not in self.char_readings[ch]:
                        self.char_readings[ch].append(s)

    def save_learned(self):
        if not self.learned_path:
            return
        d = os.path.dirname(self.learned_path)
        if d:
            os.makedirs(d, exist_ok=True)
        json.dump({"pinyin": self.learned, "corrections": self.corrections},
                  open(self.learned_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------- 查询 ----------
    def pinyin_of(self, word):
        return self.words.get(word) or self.learned.get(word)

    def candidates(self, text, n=5, min_ratio=0.5):
        text = norm_text(text)
        if not text:
            return []
        if text in self.corrections:
            w = self.corrections[text]
            return [Match(w, self.words.get(w, ""), 1.0, True)]
        head = []
        if text in self.words:
            head = [Match(text, self.words[text], 1.0, True)]
        if head and n == 1:
            return head  # 已精确命中时不再遍历全部词库，匹配结果与原版一致。
        out = []
        for w, py in self.words.items():
            if abs(len(w) - len(text)) > 2:
                continue
            r = difflib.SequenceMatcher(None, text, w).ratio()
            if r < min_ratio:
                continue
            if len(w) == len(text):
                r += 0.06
            if w[:1] == text[:1]:
                r += 0.04
            if w[-1:] == text[-1:]:
                r += 0.02
            out.append(Match(w, py, r, False))
        out.sort(key=lambda m: (-m.score, len(m.word)))
        seen = {head[0].word} if head else set()
        merged = head + [m for m in out if m.word not in seen]
        return merged[:n]

    def best(self, text, min_score=0.62):
        cs = self.candidates(text, n=1, min_ratio=min_score - 0.1)
        if cs and cs[0].score >= min_score:
            return cs[0]
        return None

    # ---------- 多音字变体 ----------
    def variants(self, word, limit=24):
        base = self.pinyin_of(word)
        if not base:
            return []
        syl = base.split()
        if not syl or len(syl) != len(word):
            return [base]
        options = []
        for ch, s in zip(word, syl):
            extra = []
            for cand, cnt in sorted(self._char_freq.get(ch, {}).items(), key=lambda kv: -kv[1]):
                if cand != s and cand not in extra:
                    extra.append(cand)
            options.append([s] + extra[:2])
        results = []

        def rec(i, cur):
            if len(results) >= limit:
                return
            if i == len(options):
                v = " ".join(cur)
                if v != base:
                    results.append(v)
                return
            for r in options[i]:
                rec(i + 1, cur + [r])

        rec(0, [])
        return [base] + results

# 半角标点 -> 全角(中文习惯), 用于"带标点打字"
_PUNCT_MAP = {
    ",": "，", ".": "。", "?": "？", "!": "！", ":": "：", ";": "；",
    "(": "（", ")": "）", "~": "～", "-": "—", "[": "【", "]": "】",
}


def punct_norm(s):
    """保留中文与常用标点, 去掉空白; 半角标点转全角。"""
    if not s:
        return ""
    s = s.translate(FULLWIDTH)
    s = re.sub(r"[\s\u3000]+", "", s)
    out = []
    for ch in s:
        if ch in _PUNCT_MAP:
            out.append(_PUNCT_MAP[ch])
        elif "\u4e00" <= ch <= "\u9fff" or ch in "，。？！：；、（）《》「」『』【】—…·～“”‘’":
            out.append(ch)
    return "".join(out)
