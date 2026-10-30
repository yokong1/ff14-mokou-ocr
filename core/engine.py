"""核心循环: 截屏 -> OCR -> 匹配词库 -> 输入拼音 -> 回车 -> 验证 -> 纠错学习。"""
import difflib
import os
import time

from . import win
from .candidates import parse_candidates, signature
from .lookup import has_latin, norm_loose, norm_text, plain_pinyin, punct_norm


def _now():
    return time.time()


def _overlap(a, b):
    """两个矩形 (x,y,w,h) 的交集面积占 a 的比例"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / float(max(1.0, aw * ah))


def _has_cjk(s):
    return any("\u4e00" <= c <= "\u9fff" for c in (s or ""))


class Target:
    __slots__ = ("text", "match", "x", "y", "w", "h", "raw")

    def __init__(self, text, match, box, raw=""):
        self.text = text
        self.match = match
        self.x, self.y, self.w, self.h = box
        self.raw = raw or text

    def __repr__(self):
        return "Target(%s@%s,%s -> %s)" % (self.text, round(self.x), round(self.y), self.match)


class Engine:
    def __init__(self, cfg, ocr, lex, kb, log=None, on_event=None):
        self.cfg = cfg
        self.ocr = ocr
        self.lex = lex
        self.kb = kb
        self.log = log or (lambda *a: None)
        self.on_event = on_event or (lambda *a: None)
        self.stop_flag = False
        self.pause_flag = False

        self.plans = {}          # OCR 文本 -> [(词, 拼音), ...] 尝试阶梯
        self.attempt = {}        # OCR 文本 -> [次数, 最近时间]
        self.skip_until = {}     # OCR 文本 -> 跳过到何时
        self.last_action = 0.0
        self.pending_typed = ""
        self.stats = {"typed": 0, "killed": 0, "unknown": 0, "miss": 0}
        self.last_frame = None
        self._hwnd = None
        self._hwnd_ttl = 0.0
        self._tight = None
        self._tight_ttl = 0.0
        self._full_scan_ts = 0.0
        self._unicode_read_cache = None
        self._unknown_logged = {}
        self._input_fail = 0
        self._afk_ts = _now()
        self._afk_sends = 0
        self.preview_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".cache", "preview.png")

    # ---------------- 窗口 / 区域 ----------------
    def hwnd(self):
        now = _now()
        if self._hwnd and now < self._hwnd_ttl and win.is_window(self._hwnd):
            return self._hwnd
        kw = self.cfg.get("window_title", "")
        w = win.find_window(kw) if kw else None
        self._hwnd = w[0] if w else None
        self._hwnd_ttl = now + 2.0
        return self._hwnd

    def window_rect(self):
        h = self.hwnd()
        return win.client_rect(h) if h else None

    def region(self):
        r = self.cfg.get("scan_region")
        if r and len(r) == 4 and r[2] > 0 and r[3] > 0:
            return tuple(int(v) for v in r)
        rc = self.window_rect()
        if not rc:
            return None
        x, y, cw, ch = rc
        top = self.cfg.get("scan_top_ratio", 0.0)
        bot = self.cfg.get("scan_bottom_ratio", 1.0)
        return (x, y + int(ch * top), cw, int(ch * (bot - top)))

    def narrow(self, bbox):
        """用上一帧目标框生成下一帧的小扫描窗, 大幅提速"""
        if not self.cfg.get("auto_narrow", True) or not bbox:
            self._tight = None
            return
        full = self.region()
        if not full:
            return
        fx, fy, fw, fh = full
        m = int(self.cfg.get("narrow_margin", 26))
        x, y, w, h = bbox
        x0, y0 = max(fx, int(x) - m), max(fy, int(y) - m)
        x1, y1 = min(fx + fw, int(x + w) + m), min(fy + fh, int(y + h) + m)
        if x1 - x0 < 16 or y1 - y0 < 16:
            return
        self._tight = (x0, y0, x1 - x0, y1 - y0)
        self._tight_ttl = _now() + self.cfg.get("narrow_seconds", 1.5)

    def scan_rect(self):
        now = _now()
        if (self._tight and now < self._tight_ttl
                and now-self._full_scan_ts < self.cfg.get("full_scan_interval", .6)):
            return self._tight
        self._full_scan_ts = now
        return self.region()

    # ---------------- 解析 ----------------
    def parse(self, result):
        targets = []
        black = [b for b in self.cfg.get("blacklist", []) if b]
        lo = self.cfg.get("min_len", 2)
        hi = max(self.cfg.get("max_len", 12), max(map(len, self.lex.words), default=0))
        skip_rects = self.skip_rects()
        origin = result.get("region")
        ox, oy = origin[:2] if origin else (0, 0)
        for ln in result.get("lines", []):
            raw = ln.get("t", "")
            text = norm_text(raw)
            if not text or not (lo <= len(text) <= hi):
                continue
            # 输入框里的拼音/半成品文字, 不是怪名
            if has_latin(text):
                continue
            if text[0].isdigit():
                continue
            if any(b in text for b in black):
                continue
            if not any("\u4e00" <= c <= "\u9fff" for c in text):
                continue
            # 落在输入框 / 候选框区域里的, 不是怪名
            box = (ln["x"], ln["y"], ln["w"], ln["h"])
            screen_box = (ox+box[0], oy+box[1], box[2], box[3])
            if any(_overlap(screen_box, r) > 0.5 for r in skip_rects):
                continue
            m = self.lex.best(text)
            if not m:
                self.stats["unknown"] += 1
                now = _now()
                if now - self._unknown_logged.get(text, 0) > 5.0:
                    self._unknown_logged[text] = now
                    self.log("词库外文本: %r" % text, "warn")
                continue
            targets.append(Target(text, m, box))
        return targets

    def skip_rects(self):
        """输入框 / 候选面板区域, 里面出现的字不能被当成怪名"""
        out = []
        for key in ("candidate_region", "input_region"):
            r = self.cfg.get(key)
            if r and len(r) == 4 and r[2] > 8 and r[3] > 8:
                out.append(tuple(int(v) for v in r))
        cb = getattr(self, "_cand_box", None)
        if cb and cb[2] > 8 and cb[3] > 8:
            out.append(cb)
        return out

    def pick(self, targets):
        now = _now()
        live = [t for t in targets if self.skip_until.get(t.text, 0) < now]
        if not live:
            return None
        if self.cfg.get("priority", "lowest") == "lowest":
            live.sort(key=lambda t: t.y + t.h, reverse=True)
        return live[0]

    # ---------------- 尝试阶梯 ----------------
    def plan_for(self, text):
        if text not in self.plans:
            seq = []
            for m in self.lex.candidates(text, n=self.cfg.get("candidate_depth", 4)):
                for v in self.lex.variants(m.word, limit=self.cfg.get("variant_per_word", 3)):
                    item = (m.word, v)
                    if item not in seq:
                        seq.append(item)
            # 词库里精确命中也至少要试够次数(可能是回车时序问题, 重试就能过)
            need = self.cfg.get("min_attempts", 3)
            if seq and len(seq) < need:
                while len(seq) < need:
                    seq.append(seq[0])
            self.plans[text] = seq
        return self.plans[text]

    def _stale(self, text):
        """同一个词隔了一段时间又出现, 视为新怪, 尝试次数归零"""
        rec = self.attempt.get(text)
        if not rec:
            return
        if _now() - rec[1] > self.cfg.get("attempt_reset_seconds", 2.5):
            self.attempt.pop(text, None)

    def next_attempt(self, text):
        self._stale(text)
        seq = self.plan_for(text)
        if not seq:
            return None
        i = self.attempt.get(text, [0, 0])[0]
        limit = min(len(seq), self.cfg.get("max_attempts", 4))
        if i >= limit:
            self._give_up(text)
            return None
        return seq[i]

    def _bump(self, text):
        rec = self.attempt.get(text)
        n = (rec[0] if rec else 0) + 1
        self.attempt[text] = [n, _now()]
        return n

    def _give_up(self, text, why="尝试次数用尽"):
        self.skip_until[text] = _now() + self.cfg.get("skip_seconds", 8.0)
        self.attempt.pop(text, None)
        self.plans.pop(text, None)
        self.log("%s, 暂时跳过 %r" % (why, text), "warn")

    # ---------------- 输入框闭环 ----------------
    def input_rect(self):
        r = self.cfg.get("input_region")
        if r and len(r) == 4 and r[2] > 8 and r[3] > 8:
            return tuple(int(v) for v in r)
        return None

    def read_input(self):
        """读输入框里现有的内容; 没有框输入框就返回 None"""
        rect = self.input_rect()
        if not rect:
            return None
        scale = self.cfg.get("input_scale", 2.0)
        res = self.ocr.ocr_region(*rect, scale)
        if res.get("ok") and not res.get("lines") and scale != 1.0:
            res = self.ocr.ocr_region(*rect, 1.0)
        if not res.get("ok"):
            return None
        return "".join(norm_loose(ln.get("t", "")) for ln in res.get("lines", []))

    def read_input_retry(self, times=4, gap=0.12):
        """OCR 可能漏读, 多读几次再判定"""
        for i in range(times):
            t = self.read_input()
            if t:
                return t
            if i + 1 < times:
                time.sleep(gap)
        return self.read_input()

    # ---------------- 上屏(把拼音变成汉字) ----------------
    def commit_word(self, word):
        """打字之后把拼音提交成汉字, 并用输入框当裁判判断提交得对不对。
        返回 (状态, 框里内容, 用到的键)
          状态: 'ok' 已是目标词 | 'wrong' 上屏了但不是目标词 | 'none' 没反应
        """
        keys = self.cfg.get("commit_keys", ["space", "1", "enter"])
        before = self.read_input_retry() or ""
        for k in keys:
            self.kb.press(k)
            time.sleep(self.cfg.get("commit_wait", 0.18))
            after = self.read_input_retry() or ""
            if after == before:
                continue
            if not _has_cjk(after):
                # 还是拼音(或只多了空格), 说明没上屏
                self.log("  按 [%s] 没上屏(框里还是字母)" % k, "info")
                before = after
                continue
            r = difflib.SequenceMatcher(None, after, word).ratio()
            if after == word or r >= self.cfg.get("commit_match_ratio", 0.6):
                if self.cfg.get("working_commit") != k:
                    self.cfg["working_commit"] = k
                    self.log("上屏键 [%s] 生效" % k, "info")
                return "ok", after, k
            self.log("  上屏成 %r, 不是目标 %r (相似度 %.2f)" % (after, word, r), "warn")
            return "wrong", after, k
        return "none", self.read_input_retry() or "", None

    def clear_input(self):
        if self.input_rect():
            for _ in range(self.cfg.get("clear_rounds", 4)):
                left = self.read_input()
                if left == "":
                    self.pending_typed = ""
                    return
                if left is None:
                    break
                self.kb.backspace(min(len(left) + 2, 40))
                time.sleep(0.04)
            self.pending_typed = ""
            return
        n = len(self.pending_typed)
        if n:
            self.kb.backspace(min(n + 2, 48))
            self.pending_typed = ""

    # ---------------- 发送 ----------------
    def enter_modes_to_try(self):
        we = self.cfg.get("working_enter")
        all_modes = ["vk", "post", "postchar", "scan", "ime"]
        if we:
            return [we] + [m for m in all_modes if m != we]
        return all_modes

    def digit_sweep(self, word, pinyin):
        """候选第1位不是目标词时: 重新打一遍再按数字 2~5 逐个试,
        每试一次都读输入框核对, 选对了才发送"""
        for d in str(self.cfg.get("sweep_digits", "2345")):
            self.clear_input()
            self.kb.type_text(pinyin)
            time.sleep(self.cfg.get("after_type_wait", 0.20))
            self.kb.press(d)
            time.sleep(self.cfg.get("commit_wait", 0.18))
            boxed = self.read_input_retry() or ""
            if not _has_cjk(boxed):
                continue
            r = difflib.SequenceMatcher(None, boxed, word).ratio()
            self.log("  试数字 %s -> %r (相似度 %.2f)" % (d, boxed, r), "info")
            if boxed == word or r >= self.cfg.get("commit_match_ratio", 0.6):
                return True
        return False

    def send_message(self):
        """按回车发送。框了输入框就用它当裁判: 一直按到框变空为止,
        并且逐个尝试不同的"回车通路"(字母能进去不代表回车能进去)。"""
        rect = self.input_rect()
        if not rect:
            self.kb.enter()
            return 1
        gap = self.cfg.get("enter_gap", 0.13)
        modes = self.enter_modes_to_try()
        tries = self.cfg.get("send_enters", 4)
        last = None
        for i in range(tries):
            mode = modes[min(i, len(modes) - 1)]
            last = mode
            self.kb.enter_via(mode)
            time.sleep(gap)
            left = self.read_input()
            if not left:
                if self.cfg.get("working_enter") != mode:
                    self.cfg["working_enter"] = mode
                    self.log("回车通路 [%s] 生效" % mode, "info")
                return i + 1
        self.log("  换了 %d 种回车方式都没发出去, 输入框还留着 %r (最后用的 %s)"
                 % (tries, self.read_input(), last), "warn")
        return tries

    # ---------------- 按键通道探测 ----------------
    def modes_to_try(self):
        mode = self.cfg.get("send_mode", "auto")
        if mode != "auto":
            return [mode]
        wm = self.cfg.get("working_mode")
        if wm:
            return [wm] + [m for m in ("vk", "scan", "post", "postchar") if m != wm]
        return ["vk", "scan", "post", "postchar"]

    def type_with_probe(self, p, hwnd):
        """把 p 敲进去, 并确认真的进去了; 返回生效的方式
        读不到输入框时不武断判失败(OCR 会漏读), 而是继续发送交给"怪死没死"来判定"""
        self.kb.bind(hwnd)
        has_box = bool(self.input_rect())
        last = "vk"
        for m in self.modes_to_try():
            last = m
            self.kb.send_mode = m
            if has_box:
                self.clear_input()
            self.pending_typed = ""
            self.kb.type_text(p)
            self.pending_typed = p
            time.sleep(self.cfg.get("after_type_wait", 0.22))
            if not has_box:
                return m
            got = self.read_input_retry()
            if got:
                if self.cfg.get("working_mode") != m:
                    self.cfg["working_mode"] = m
                    self.log("按键方式 [%s] 生效" % m, "info")
                return m
            self.log("按键方式 [%s] 打完读不到输入框内容, 换下一种试试" % m, "warn")
            self.pending_typed = ""
        # 全都读不到: 不放弃, 用最后一种继续
        self.kb.send_mode = self.cfg.get("working_mode") or last
        self.log("几种方式都读不到输入框(可能是OCR看不清), 继续用 [%s] 往下走, 由'怪死没死'判定"
                 % self.kb.send_mode, "warn")
        return self.kb.send_mode + "?"

    def select_candidate(self, band_rect, word):
        res = self.ocr.ocr_region(*band_rect, self.cfg.get("candidate_scale", 1.0))
        best = None
        for ln in res.get("lines", []):
            t = norm_text(ln.get("t", ""))
            if len(t) < 2 or not t[0].isdigit():
                continue
            num, body = int(t[0]), t[1:]
            r = difflib.SequenceMatcher(None, body, word).ratio()
            if r > 0.62 and (best is None or r > best[1]):
                best = (num, r)
        return best[0] if best else None

    # ---------------- 候选词面板 ----------------
    def candidate_rect(self):
        r = self.cfg.get("candidate_region")
        if r and len(r) == 4 and r[2] > 8 and r[3] > 8:
            return tuple(int(v) for v in r)
        cb = getattr(self, "_cand_box", None)
        if cb and cb[2] > 8 and cb[3] > 8:
            return cb
        # 候选面板就贴在输入框正下方(见游戏截图), 用输入框位置推导
        ir = self.input_rect()
        if ir:
            return (ir[0], ir[1] + ir[3] - 4, ir[2], max(60, ir[3] * 5))
        return self.window_rect()

    def read_candidates(self):
        rect = self.candidate_rect()
        if not rect:
            return [], None, None
        res = self.ocr.ocr_region(*rect, self.cfg.get("candidate_scale", 1.0))
        if not res.get("ok"):
            return [], None, None
        cands, page, pages = parse_candidates(res)
        if cands and not self.cfg.get("candidate_region"):
            # 缓存候选面板位置, 下次只扫这一小块(快很多)
            x0 = min(c.x for c in cands)
            y0 = min(c.y for c in cands)
            x1 = max(c.x + c.w for c in cands)
            y1 = max(c.y + c.h for c in cands)
            m = 30
            self._cand_box = (rect[0] + int(x0) - m, rect[1] + int(y0) - m,
                              int(x1 - x0) + 2 * m, int(y1 - y0) + 2 * m)
        return cands, page, pages

    def match_candidate(self, cands, word):
        """在候选里找出目标词, 返回 (序号, 相似度)"""
        best = None
        for c in cands:
            r = difflib.SequenceMatcher(None, c.word, word).ratio()
            if c.word == word:
                r = 1.0
            if r >= self.cfg.get("candidate_match_ratio", 0.66):
                if best is None or r > best[1]:
                    best = (c.num, r, c.word)
        return best

    def page_keys(self):
        wk = self.cfg.get("working_page_key")
        keys = self.cfg.get("page_keys", ["equal", "minus", "pgdn", "rbracket", "lbracket"])
        if wk:
            return [wk] + [k for k in keys if k != wk]
        return keys

    def page_forward(self, before_sig):
        """翻页, 并确认真的翻动了; 自动探测哪个键能翻页并记住"""
        for key in self.page_keys():
            self.kb.press(key)
            time.sleep(self.cfg.get("page_wait", 0.14))
            cands, page, pages = self.read_candidates()
            if cands and signature(cands) != before_sig:
                if self.cfg.get("working_page_key") != key:
                    self.cfg["working_page_key"] = key
                    self.log("翻页键 [%s] 生效 (第 %s/%s 页)" % (key, page, pages), "info")
                else:
                    self.log("  第 %s/%s 页" % (page, pages), "info")
                return cands
        return None

    def solve_candidate(self, word):
        """读候选 -> 找到目标词所在序号; 找不到就翻页。
        返回 (是否命中, 序号或 None, 是否出现过候选面板)"""
        cands, page, pages = self.read_candidates()
        if not cands:
            return False, None, False
        hit = self.match_candidate(cands, word)
        if hit:
            self.log("  候选第 %d 位命中 %s" % (hit[0], hit[2]), "info")
            return True, hit[0], True
        self.log("  本页无此词: %s" % [c.word for c in cands], "info")
        for _ in range(max(1, self.cfg.get("max_pages", 3) - 1)):
            cands2 = self.page_forward(signature(cands))
            if not cands2:
                break
            cands = cands2
            hit = self.match_candidate(cands, word)
            if hit:
                self.log("  翻页后第 %d 位命中 %s" % (hit[0], hit[2]), "info")
                return True, hit[0], True
        return False, None, True

    def submit_styles(self):
        styles = self.cfg.get("submit_styles", ["plain", "space", "digit1", "unicode", "unicode_post"])
        ws = self.cfg.get("working_submit")
        if ws and ws in styles:
            return [ws] + [s for s in styles if s != ws]
        return styles

    def style_for(self, text):
        st = self.submit_styles()
        i = self.attempt.get(text, [0, 0])[0]
        return st[min(i, len(st) - 1)]

    def type_unicode_probe(self, word, hwnd, post=False, ime_char=False):
        """直接把汉字塞进输入框(绕开输入法)"""
        self.kb.bind(hwnd)
        self._unicode_read_cache = None
        if not self.input_rect():
            if post:
                self.kb.post_unicode(word, ime_char=ime_char)
            else:
                self.kb.type_unicode(word)
            return "unicode"
        candidates = [("post", ime_char)] if post else [("send", False)]  # 只打一次, 避免重复输入
        for kind, ime in candidates:
            self.clear_input()
            if kind == "post":
                self.kb.post_unicode(word, ime_char=ime)
            else:
                self.kb.type_unicode(word)
            time.sleep(self.cfg.get("after_type_wait", 0.22))
            got = self.read_input_retry()
            self._unicode_read_cache = (word, got, time.monotonic())
            if got and difflib.SequenceMatcher(None, norm_text(got), norm_text(word)).ratio() >= 0.6:
                style = "unicode_post" if post else "unicode"
                if self.cfg.get("working_submit") != style:
                    self.cfg["working_submit"] = style
                    self.log("提交方式 [%s] 生效" % style, "info")
                return style
            if got:
                # 框里内容对不上也照打不误: 用"怪死没死"判定, 不要在这里自杀
                self.log("  框内=%r 与目标不同, 仍继续发送(交给验证判断)" % got, "warn")
                return "unicode?" if not post else "unicode_post?"
        return None

    def act(self, t, region):
        cfg = self.cfg
        att = self.next_attempt(t.text)
        if att is None:
            return
        word, py = att
        p = plain_pinyin(py)
        typed_text = word
        if cfg.get("keep_punct", True):
            raw = getattr(t, "raw", "") or ""
            if raw and raw != word and norm_text(raw) == word:
                typed_text = raw
                self.log("  识别原文带标点: %r, 按原样打" % raw, "info")

        if cfg.get("dry_run"):
            n = self._bump(t.text)
            self.log("[模拟] %s -> 词=%s 拼音=%s (第%d次)" % (t.text, word, p, n), "hit")
            self.stats["typed"] += 1
            self.last_action = _now()
            return

        hwnd = self.hwnd()
        if not hwnd:
            self.log("找不到游戏窗口", "err")
            return
        if win.foreground() != hwnd:
            win.focus(hwnd)
            time.sleep(cfg.get("focus_wait", 0.10))
        if win.foreground() != hwnd:
            win.focus(hwnd)
            time.sleep(0.25)
        if win.foreground() != hwnd:
            self.log("游戏窗口没切到前台(当前前台 hwnd=%s, 目标=%s), 按键不会生效。"
                     "请手动点一下游戏画面, 或用管理员身份启动本工具"
                     % (win.foreground(), hwnd), "err")
            self._input_fail += 1
            if self._input_fail >= cfg.get("pause_after_fail", 3):
                self.pause_flag = True
                self.log("连续 %d 次没能把游戏切到前台, 已自动暂停" % self._input_fail, "err")
            time.sleep(0.4)
            return

        # FFXIV 等游戏需要先按一次 Enter 激活输入行, 否则字符打不进去
        if cfg.get("open_with_enter", True):
            self.kb.enter()
            time.sleep(cfg.get("open_wait", 0.22))
        style = self.style_for(t.text)
        commit_wait = cfg.get("commit_wait", 0.18)
        if style in ("unicode", "unicode_post"):
            used = self.type_unicode_probe(typed_text, hwnd, post=(style == "unicode_post"))
        else:
            used = self.type_with_probe(p, hwnd)
            if used:
                handled = False
                # 优先: 读游戏候选面板, 找到目标词在第几位, 按数字键选中
                if cfg.get("candidate_mode", True):
                    found, num, composing = self.solve_candidate(word)
                    if found and num and num > 1:
                        self.kb.press(str(num))
                        time.sleep(cfg.get("after_digit_wait", 0.08))
                        self.log("  候选面板里第 %d 位, 已按下 %d 选中" % (num, num), "info")
                        handled = True
                    elif found and num == 1:
                        self.log("  候选面板里就在第 1 位", "info")
                    elif composing:
                        self.log("  候选面板里没找到 %s, 换个方式上屏" % word, "warn")
                if not handled:
                    if style == "space":
                        self.kb.press("space")
                        time.sleep(commit_wait)
                    elif style == "digit1":
                        self.kb.press("1")
                        time.sleep(commit_wait)
        if used is None:
            self._input_fail += 1
            self.log("这一步打不进去(方式=%s)。可能原因: 游戏以管理员运行, 本工具需要管理员身份" % style, "err")
            if self._input_fail >= cfg.get("pause_after_fail", 3):
                self.pause_flag = True
                self.log("连续 %d 次按键无效, 已自动暂停。请看上面日志" % self._input_fail, "err")
            return
        self._input_fail = 0

        # 输入框内容记录(游戏若只认拼音, 这一步只是提示)
        if self.input_rect():
            recent = self._unicode_read_cache
            self._unicode_read_cache = None
            if (style in ("unicode", "unicode_post") and recent and recent[0] == typed_text
                    and time.monotonic()-recent[2] < .25):
                boxed = recent[1] or ""  # 刚读过同一输入，诊断日志不再重复做 OCR。
            else:
                boxed = self.read_input_retry() or ""
            if boxed:
                r = difflib.SequenceMatcher(None, boxed, word).ratio()
                self.log("  框内=%r 与目标相似度 %.2f (提交方式=%s)" % (boxed, r, style), "info")
            self.send_message()
        else:
            self.kb.enter()

        self.pending_typed = ""
        self.last_action = _now()
        self.stats["typed"] += 1
        n = self._bump(t.text)
        self.log("输入 %s  [%s]  识别=%s (第%d次)" % (p, word, t.text, n), "hit")

        if not cfg.get("verify", True):
            if n >= min(len(self.plan_for(t.text)), cfg.get("max_attempts", 4)):
                self._give_up(t.text)
            return

        time.sleep(cfg.get("verify_wait", 0.32))
        res = self.ocr.ocr_region(*region, cfg.get("ocr_scale", 1.0))
        alive = False
        for ln in res.get("lines", []):
            txt = norm_text(ln.get("t", ""))
            if txt and difflib.SequenceMatcher(None, txt, t.text).ratio() > 0.62:
                alive = True
                break
        if alive:
            self.stats["miss"] += 1
            self.log("  没打掉 (%d/%d), 这次用的提交方式=%s, 下次换一种"
                     % (n, min(len(self.plan_for(t.text)), cfg.get("max_attempts", 4)), style), "miss")
            if n >= min(len(self.plan_for(t.text)), cfg.get("max_attempts", 4)):
                self._give_up(t.text)
            return

        self.stats["killed"] += 1
        self.log("  打掉了 %s (提交方式=%s)" % (word, style), "done")
        if self.cfg.get("working_submit") != style:
            self.cfg["working_submit"] = style
            self.log("锁定提交方式 [%s]" % style, "info")
        self.attempt.pop(t.text, None)
        if word != t.text or not t.match.exact:
            self.lex.corrections[t.text] = word
            self.lex.save_learned()
            self.plans.pop(t.text, None)
            self.log("  学习纠错: %r -> %s" % (t.text, word), "learn")

    def anti_afk(self):
        """防掉线: 敲一下输入行, 不发送, 再删掉"""
        hwnd = self.hwnd()
        if hwnd and win.foreground() != hwnd:
            win.focus(hwnd)
            time.sleep(0.15)
        try:
            self.kb.enter()          # 打开输入行
            time.sleep(0.12)
            self.kb.type_text("a")   # 只敲一个字
            time.sleep(0.06)
            self.kb.backspace(1)     # 只删一个字
            time.sleep(0.06)
            self.kb.enter()          # 关闭输入行(空内容不会发出去)
            self.log("  防掉线: 已活动一次", "info")
        except Exception as e:
            self.log("  防掉线失败: %s" % e, "warn")
        self._afk_ts = _now()
        self._afk_sends = self.stats["typed"]

    # ---------------- 主循环 ----------------
    def run(self):
        cfg = self.cfg
        poll = cfg.get("poll_interval", 0.08)
        scale = cfg.get("ocr_scale", 1.0)
        preview_every = max(1, cfg.get("preview_every", 3))
        n = 0
        kw = cfg.get("window_title", "")
        w = win.find_window(kw) if kw else None
        if w:
            self.log("目标窗口: 标题=%r 客户区=%s" % (w[1], win.client_rect(w[0])))
        else:
            self.log("!! 找不到窗口: 关键字=%r —— 请在上方下拉框选择游戏窗口" % kw, "err")
        self.log("引擎启动 (dry_run=%s) 扫描区域=%s 输入框区域=%s"
                 % (cfg.get("dry_run"), self.region(), cfg.get("input_region")))
        while not self.stop_flag:
            if self.pause_flag:
                time.sleep(0.1)
                continue
            n += 1
            if cfg.get("anti_afk", True) and not cfg.get("dry_run"):
                _sec = cfg.get("anti_afk_seconds", 10)
                _cnt = cfg.get("anti_afk_every_sends", 7)
                if (_now() - self._afk_ts > _sec) or (self.stats["typed"] - self._afk_sends >= _cnt):
                    self.anti_afk()
            try:
                region = self.scan_rect()
                if not region:
                    self.log("找不到游戏窗口, 等待中...", "warn")
                    time.sleep(0.8)
                    continue
                h = self.hwnd()
                if h and win.is_minimized(h):
                    time.sleep(0.3)
                    continue
                res = self.ocr.ocr_names(*region, scale, lexicon=self.lex)
                self.last_frame = res
                if cfg.get("preview") and n % preview_every == 0:
                    try:
                        os.makedirs(os.path.dirname(self.preview_path), exist_ok=True)
                        self.ocr.save_last(self.preview_path)
                        self.on_event("preview", res)
                    except Exception:
                        pass
                if not res.get("ok"):
                    self.log("OCR 失败: %s" % res.get("err"), "err")
                    time.sleep(0.3)
                    continue
                targets = self.parse(res)
                self.on_event("frame", {"region": region, "result": res, "targets": targets})
                if targets:
                    x0 = min(t.x for t in targets)
                    y0 = min(t.y for t in targets)
                    x1 = max(t.x + t.w for t in targets)
                    y1 = max(t.y + t.h for t in targets)
                    self.narrow((region[0]+x0, region[1]+y0, x1 - x0, y1 - y0))
                else:
                    self._tight = None
                if not targets:
                    time.sleep(poll)
                    continue
                if _now() - self.last_action < cfg.get("cooldown", 0.5):
                    time.sleep(0.02)
                    continue
                t = self.pick(targets)
                if not t:
                    time.sleep(poll)
                    continue
                self.act(t, region)
            except Exception as e:
                self.log("循环异常: %s: %s" % (type(e).__name__, e), "err")
                time.sleep(0.4)
        self.log("引擎已停止 %s" % self.stats)
