"""识别打怪助手 - 图形界面 (tkinter, 无第三方依赖)"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from core import win
from core.engine import Engine
from core.keys import Keyboard
from core.lookup import Lexicon, norm_loose, norm_text
from core.ocrclient import OcrClient

APP_NAME = "ff14 mokou ocr"
APP_VERSION = "0.9.2"
SCAN_REGION_HELP = "框选怪物名字可能出现的完整范围，尽量不要框取多余区域。"
INPUT_REGION_HELP = "只框选文本输入框内的文字区域，不要把左侧输入法的“中”框进去。"

CONFIG_PATH = os.path.join(BASE, "config.json")
LEX_PATH = os.path.join(BASE, "词条总表.tsv")
LEARNED_PATH = os.path.join(BASE, "data", "learned.json")

DEFAULT_CFG = {
    "window_title": "",
    "scan_region": None,
    "scan_top_ratio": 0.0,
    "scan_bottom_ratio": 1.0,
    "ocr_scale": 1.0,
    "poll_interval": 0.08,
    "cooldown": 0.5,
    "after_type_wait": 0.16,
    "after_digit_wait": 0.08,
    "page_wait": 0.12,
    "commit_wait": 0.12,
    "verify": True,
    "verify_wait": 0.32,
    "focus_wait": 0.06,
    "key_delay": 0.008,
    "send_mode": "auto",
    "working_mode": None,
    "pause_after_fail": 8,
    "candidate_mode": False,
    "candidate_region": None,
    "candidate_band": [0.0, 0.50, 1.0, 1.0],
    "candidate_match_ratio": 0.66,
    "candidate_scale": 1.5,
    "commit_keys": ["space", "1", "enter"],
    "commit_match_ratio": 0.6,
    "digit_sweep": True,
    "sweep_digits": "2345",
    "working_commit": None,
    "submit_styles": ["unicode", "unicode_post", "plain", "space", "digit1"],
    "working_submit": None,
    "page_key": "equal",
    "page_keys": ["equal", "minus", "pgdn", "rbracket", "lbracket"],
    "working_page_key": None,
    "max_pages": 3,
    "submit": "auto",
    "working_enter": None,
    "input_region": None,
    "input_scale": 2.5,
    "send_enters": 4,
    "enter_gap": 0.13,
    "clear_rounds": 4,
    "min_attempts": 3,
    "anti_afk": True,
    "anti_afk_seconds": 10,
    "anti_afk_every_sends": 7,
    "open_with_enter": True,
    "open_wait": 0.22,
    "hide_on_start": False,
    "config_version": 8,
    "candidate_depth": 4,
    "variant_per_word": 3,
    "max_attempts": 4,
    "skip_seconds": 8.0,
    "min_len": 2,
    "max_len": 14,
    "priority": "lowest",
    "blacklist": ["打倒所有敌兵", "得分", "奖励加成", "发送已输入的文字", "清空所有已输入的文字",
                  "退出任务", "按", "输入法", "回车"],
    "dry_run": False,
    "preview": True,
    "preview_every": 3,
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as stream:
                cfg.update(json.load(stream))
        except Exception:
            pass
    v = cfg.get("config_version", 0)
    if v < 8:
        cfg["pause_after_fail"] = 8
        cfg["input_scale"] = 2.5
        cfg["config_version"] = 8
        save_cfg(cfg)
        v = 8
    if v < 7:
        cfg["submit_styles"] = ["unicode", "unicode_post", "plain", "space", "digit1"]
        cfg["working_submit"] = "unicode"
        cfg["candidate_mode"] = False
        cfg["config_version"] = 7
        save_cfg(cfg)
        v = 7
    if v < 6:
        if v < 3:
            cfg["min_attempts"] = 3
            cfg["submit"] = "auto"
        cfg["candidate_mode"] = True           # 读候选面板, 按数字键选字
        cfg["commit_keys"] = ["space", "1", "enter"]
        cfg["candidate_scale"] = 1.5
        if cfg.get("candidate_band") in (None, [0.28, 0.68, 0.62, 0.97], [0.25, 0.62, 0.65, 0.97]):
            cfg["candidate_band"] = list(DEFAULT_CFG["candidate_band"])
        cfg["config_version"] = 6
        save_cfg(cfg)
    cfg["candidate_mode"] = False
    cfg["candidate_region"] = None
    return cfg


def save_cfg(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as stream:
        json.dump(cfg, stream, ensure_ascii=False, indent=1)


class ToolTip:
    """仅在鼠标停留时展示区域说明，不改变选区或输入行为。"""
    def __init__(self, widget, text, delay=350):
        self.widget, self.text, self.delay = widget, text, delay
        self.after_id = None
        self.tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, event=None):
        self._hide()
        self.after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        self.after_id = None
        if self.tip is not None or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        self.tip = tip
        tip.wm_overrideredirect(True)
        x, y = self.widget.winfo_pointerx()+12, self.widget.winfo_pointery()+20
        tip.wm_geometry("%+d%+d" % (x, y))
        tk.Label(tip, text=self.text, justify="left", wraplength=360,
                 background="#fff8dc", foreground="#263238", relief="solid",
                 borderwidth=1, padx=10, pady=7, font=("Microsoft YaHei", 10)).pack()

    def _hide(self, event=None):
        if self.after_id is not None:
            try:
                self.widget.after_cancel(self.after_id)
            except tk.TclError:
                pass
            self.after_id = None
        if self.tip is not None:
            try:
                self.tip.destroy()
            except tk.TclError:
                pass
            self.tip = None


class RegionSelector:
    """全屏框选, 返回屏幕绝对坐标 (x, y, w, h)"""

    def __init__(self, root, on_done):
        self.on_done = on_done
        self.start = None
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        try:
            self.top.attributes("-alpha", 0.3)
        except Exception:
            pass
        sw, sh = win.screen_size()
        self.top.geometry("%dx%d+0+0" % (sw, sh))
        self.cv = tk.Canvas(self.top, bg="black", highlightthickness=0, cursor="crosshair")
        self.cv.pack(fill="both", expand=True)
        self.cv.create_text(sw // 2, 40, text="拖拽框选要识别的区域    Esc 取消",
                            fill="#00ff88", font=("Microsoft YaHei", 18))
        self.rect = None
        self.cv.bind("<Button-1>", self._down)
        self.cv.bind("<B1-Motion>", self._move)
        self.cv.bind("<ButtonRelease-1>", self._up)
        self.top.bind("<Escape>", lambda e: self.top.destroy())
        self.top.focus_force()

    def _down(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.cv.delete(self.rect)
        self.rect = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=2)

    def _move(self, e):
        if self.rect and self.start:
            self.cv.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def _up(self, e):
        if not self.start:
            return
        x1, y1 = self.start
        x2, y2 = e.x, e.y
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        self.top.destroy()
        if w > 8 and h > 8:
            self.on_done((x, y, w, h))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_cfg()
        self.lex = Lexicon(LEX_PATH, LEARNED_PATH)
        # 旧版固定上限 12 会挡住词库中的长 Boss 词，按实际词库长度放宽。
        self.cfg["max_len"] = max(self.cfg.get("max_len", 12), max(map(len, self.lex.words)))
        self.ocr = None
        self.engine = None
        self.thread = None
        self.q = queue.Queue()
        self.prev_img = None
        self.last_result = None
        self.hotkey_state = {}
        self._paused_shown = False
        root.title("%s · %s" % (APP_NAME, APP_VERSION))
        root.geometry("980x740")
        # 注意: 不能置顶, 否则窗口会盖住游戏, 截屏截到的就是自己
        self._build_ui()
        self._log("词库载入: %d 条" % len(self.lex.words), "info")
        self.refresh_windows()
        self.root.after(120, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 4, "pady": 2}
        top = ttk.LabelFrame(self.root, text="1. 目标窗口")
        top.pack(fill="x", padx=6, pady=4)
        self.win_var = tk.StringVar()
        self.win_box = ttk.Combobox(top, textvariable=self.win_var, width=64, state="readonly")
        self.win_box.grid(row=0, column=0, columnspan=3, sticky="we", **pad)
        self.win_box.bind("<<ComboboxSelected>>", lambda e: self._on_window_pick())
        ttk.Button(top, text="刷新窗口", command=self.refresh_windows).grid(row=0, column=3, **pad)
        top.columnconfigure(0, weight=1)

        opt = ttk.LabelFrame(self.root, text="2. 识别区域  ← 重要: 只框住怪名字出现的那一带, 别整屏识别")
        opt.pack(fill="x", padx=6, pady=4)
        self.region_var = tk.StringVar(value="自动(窗口客户区)")
        self.region_label = ttk.Label(opt, textvariable=self.region_var, foreground="#0077cc")
        self.region_label.grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        self.region_tip = ToolTip(self.region_label, SCAN_REGION_HELP)
        ttk.Button(opt, text="🖱 拖拽框选区域", command=self.select_region).grid(row=0, column=2, **pad)
        ttk.Button(opt, text="恢复自动", command=self.reset_region).grid(row=0, column=3, **pad)
        self.prev_var = tk.BooleanVar(value=self.cfg["preview"])
        ttk.Button(opt, text="试识别一次(不按键)", command=self.test_once).grid(row=0, column=4, **pad)
        self.input_region_var = tk.StringVar(value="输入框区: 未设置")
        self.input_region_label = ttk.Label(opt, textvariable=self.input_region_var, foreground="#c06000")
        self.input_region_label.grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        self.input_region_tip = ToolTip(self.input_region_label, INPUT_REGION_HELP)
        ttk.Button(opt, text="🖱 框选输入框", command=self.select_input_region).grid(row=2, column=2, **pad)
        ttk.Button(opt, text="清除", command=self.reset_input_region).grid(row=2, column=3, **pad)
        ttk.Button(opt, text="👁 看输入框", command=self.probe_input).grid(row=2, column=4, **pad)

        opt2 = ttk.LabelFrame(self.root, text="3. 参数")
        opt2.pack(fill="x", padx=6, pady=4)
        self.dry_var = tk.BooleanVar(value=self.cfg["dry_run"])
        self.verify_var = tk.BooleanVar(value=self.cfg["verify"])
        ttk.Checkbutton(opt2, text="模拟模式(只识别不按键)", variable=self.dry_var).grid(row=0, column=0, **pad)
        ttk.Checkbutton(opt2, text="实时预览", variable=self.prev_var).grid(row=0, column=2, **pad)
        ttk.Checkbutton(opt2, text="打完验证", variable=self.verify_var).grid(row=0, column=3, **pad)
        ttk.Label(opt2, text="OCR倍率").grid(row=1, column=0, sticky="e", **pad)
        self.scale_var = tk.StringVar(value=str(self.cfg["ocr_scale"]))
        ttk.Combobox(opt2, textvariable=self.scale_var, values=["1.0", "1.5", "2.0", "2.5"], width=6,
                     state="readonly").grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(opt2, text="冷却(秒)").grid(row=1, column=2, sticky="e", **pad)
        self.cool_var = tk.StringVar(value=str(self.cfg["cooldown"]))
        ttk.Combobox(opt2, textvariable=self.cool_var, values=["0.3", "0.45", "0.5", "0.8", "1.2"], width=6,
                     state="readonly").grid(row=1, column=3, sticky="w", **pad)
        ttk.Label(opt2, text="回车发送").grid(row=2, column=0, sticky="e", **pad)
        self.submit_var = tk.StringVar(value=self.cfg.get("submit", "auto"))
        ttk.Combobox(opt2, textvariable=self.submit_var, width=8, state="readonly",
                     values=["auto", "enter", "enter2"]).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(opt2, text="按键方式").grid(row=3, column=0, sticky="e", **pad)
        self.sendmode_var = tk.StringVar(value=self.cfg.get("send_mode", "auto"))
        ttk.Combobox(opt2, textvariable=self.sendmode_var, width=8, state="readonly",
                     values=["auto", "vk", "scan", "post", "postchar"]
                     ).grid(row=3, column=1, sticky="w", **pad)

        act = ttk.Frame(self.root)
        act.pack(fill="x", padx=6, pady=4)
        self.btn_start = ttk.Button(act, text="▶ 启动 (F8)", command=self.toggle_engine)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(act, text="■ 停止 (F9)", command=self.stop_engine)
        self.btn_stop.pack(side="left", padx=4)
        self.status = tk.StringVar(value="就绪")
        ttk.Label(act, textvariable=self.status, foreground="#c00060").pack(side="left", padx=12)

        mid = ttk.Panedwindow(self.root, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=6, pady=4)
        left = ttk.LabelFrame(mid, text="3. 日志")
        mid.add(left, weight=3)
        self.log_txt = tk.Text(left, height=18, font=("Consolas", 10), bg="#101418", fg="#dddddd",
                               insertbackground="#fff", wrap="none")
        self.log_txt.pack(fill="both", expand=True)
        for tag, color in [("hit", "#7fd1ff"), ("done", "#5cff9d"), ("miss", "#ff9b6a"),
                           ("warn", "#ffd166"), ("err", "#ff6b6b"), ("learn", "#c792ea"),
                           ("info", "#aaaaaa")]:
            self.log_txt.tag_config(tag, foreground=color)
        right = ttk.LabelFrame(mid, text="4. 预览 (绿框=识别到的怪名)")
        mid.add(right, weight=2)
        self.cv = tk.Canvas(right, bg="#000000", highlightthickness=0)
        self.cv.pack(fill="both", expand=True)

    # ---------------- 窗口 ----------------
    def refresh_windows(self):
        # 保留原版可见窗口枚举；不按 FF14、测试 EXE 或标题额外过滤。
        self.wins = win.list_windows()
        items = ["%s  |  %s" % (w[1], os.path.basename(w[2]) if w[2] else "?") for w in self.wins]
        self.win_box["values"] = items
        self.win_box.set("")  # 防止刷新后旧索引落到资源管理器等其他窗口。
        cur = self.cfg.get("window_title", "")
        selected = next((i for i, w in enumerate(self.wins) if w[1].lower() == cur.lower()), None) if cur else None
        if selected is None and cur:
            selected = next((i for i, w in enumerate(self.wins) if cur.lower() in w[1].lower()), None)
        if selected is not None:
            self.win_box.current(selected)
        elif not cur and items:
            self.win_box.current(0)
            self._on_window_pick()
        self._update_region_label()

    def _on_window_pick(self):
        i = self.win_box.current()
        if 0 <= i < len(self.wins):
            self.cfg["window_title"] = self.wins[i][1]
            self.cfg["proc"] = os.path.basename(self.wins[i][2] or "")
            save_cfg(self.cfg)
            self._update_region_label()

    def _update_region_label(self):
        r = self.cfg.get("scan_region")
        h = win.find_window(self.cfg.get("window_title", "")) if self.cfg.get("window_title") else None
        cr = win.client_rect(h[0]) if h else None
        if r:
            self.region_var.set("手动区域: %s" % (tuple(r),))
        elif cr:
            self.region_var.set("自动(窗口客户区): %s" % (cr,))
        else:
            self.region_var.set("自动(未找到窗口)")
        ir = self.cfg.get("input_region")
        self.input_region_var.set("输入框区: %s" % ((tuple(ir),) if ir
                                                   else "未设置 ← 强烈建议框一下, 用来确认按键真进去了"))

    def _show_crop(self, res, tag):
        try:
            os.makedirs(os.path.join(BASE, ".cache"), exist_ok=True)
            path = os.path.join(BASE, ".cache", "probe_%s.png" % tag)
            saved = self.ocr.save_last(path)
            if not saved.get("ok"):
                self._log("预览保存失败: %s" % saved.get("err", "未知原因"), "warn")
                return
            self.prev_img = tk.PhotoImage(master=self.root, file=path)
            self._show_img()
        except (OSError, tk.TclError) as error:
            self._log("预览加载失败: %s" % error, "warn")

    def probe_input(self):
        """把输入框那块截图 + OCR 结果直接展示出来"""
        self._on_window_pick()
        self.ensure_ocr()
        rect = self.cfg.get("input_region")
        if not rect:
            messagebox.showwarning("提示", "请先「🖱 框选输入框」")
            return
        h = win.find_window(self.cfg.get("window_title", ""))
        if h:
            win.focus(h[0])
            time.sleep(0.15)
        scale = self.cfg.get("input_scale", 2.0)
        res = self.ocr.ocr_region(*rect, scale)
        if res.get("ok") and not res.get("lines") and scale != 1.0:
            res = self.ocr.ocr_region(*rect, 1.0)
        lines = [ln.get("t", "") for ln in res.get("lines", [])]
        self._log("👁 输入框 OCR: %r" % lines, "info")
        self._show_crop(res, "input")

    def snap(self):
        """把工具"看到"的画面存成图片, 用来确认截屏有没有抓到游戏"""
        self._on_window_pick()
        self.ensure_ocr()
        win.set_dpi_aware()
        sw, sh = win.screen_size()
        jobs = [("full", (0, 0, sw, sh)), ("scan", self.current_region())]
        for tag, rect in jobs:
            if not rect:
                self._log("截图失败: %s 区域为空" % tag, "err")
                continue
            self.ocr.ocr_region(*rect, 1.0)
            path = os.path.join(BASE, "shot_%s.png" % tag)
            self.ocr.save_last(path)
            self._log("已保存 %s 区域截图 -> %s  %s" % (tag, path, rect), "info")
        self._log("请把 shot_full.png 发我, 一眼就能看出截屏有没有抓到游戏", "info")

    def test_typing(self):
        """逐个试按键方式, 报告哪种真的能把字母打进游戏输入框"""
        self._on_window_pick()
        try:
            self.ensure_ocr()
        except Exception as e:
            messagebox.showerror("OCR", "OCR 服务启动失败: %s" % e)
            return
        h = win.find_window(self.cfg.get("window_title", ""))
        if not h:
            messagebox.showwarning("提示", "请先选择游戏窗口")
            return
        hwnd = h[0]
        in_rect = self.cfg.get("input_region")
        if not in_rect:
            messagebox.showwarning(
                "先框选输入框", "请先用「🖱 框选输入框」把游戏里输入文字的那个框框起来，"
                            "否则没法判断按键有没有打进去。")
            return
        win.focus(hwnd)
        time.sleep(0.25)
        fg_ok = win.foreground() == hwnd
        self._log("--- 按键测试 ---", "info")
        self._log("游戏窗口是否在前台: %s" % ("是" if fg_ok else "否 ← 这会直接导致按键无效"), 
                  "info" if fg_ok else "err")

        def read_box():
            r = self.ocr.ocr_region(*in_rect, self.cfg.get("input_scale", 2.0))
            return "".join(norm_loose(ln.get("t", "")) for ln in r.get("lines", []))

        kb = Keyboard(key_delay=0.012, send_mode="vk")
        kb.bind(hwnd)
        for mode in ["vk", "scan", "post", "postchar"]:
            kb.send_mode = mode
            kb.backspace(12)
            time.sleep(0.12)
            try:
                win.focus(hwnd)
            except Exception:
                pass
            before = read_box()
            kb.type_text("aa")
            time.sleep(0.3)
            after = read_box()
            ok = after and after != before
            self._log("  [%-9s] 打入结果=%r  %s" % (mode, after, "✅ 生效" if ok else "❌ 没反应"),
                      "done" if ok else "miss")
            kb.backspace(12)
            time.sleep(0.1)
        # ---- 回车通路测试 ----
        self._log("--- 回车通路测试 (打字用 %s) ---" % ("auto" if not self.cfg.get("working_mode")
                                                  else self.cfg["working_mode"]), "info")
        for mode in ["vk", "post", "postchar", "scan", "ime"]:
            kb.send_mode = self.cfg.get("working_mode") or "vk"
            kb.backspace(12)
            time.sleep(0.1)
            kb.type_text("aa")
            time.sleep(0.25)
            before = read_box()
            kb.enter_via(mode)
            time.sleep(0.35)
            after = read_box()
            sent = bool(before) and not after
            self._log("  [回车 %-9s] 前=%r 后=%r  %s" % (mode, before, after,
                                                      "✅ 发出去了" if sent else "❌ 无效"),
                      "done" if sent else "miss")
        kb.backspace(12)
        self._log("把生效的那种填到上面的「按键方式」里(或保持 auto 自动挑)", "info")

    def test_submit(self):
        """逐个试提交方式, 看哪种能把"琪里耶"真正放进游戏输入框"""
        self._on_window_pick()
        self.ensure_ocr()
        if not self.cfg.get("input_region"):
            messagebox.showwarning("提示", "请先「🖱 框选输入框」")
            return
        h = win.find_window(self.cfg.get("window_title", ""))
        if not h:
            messagebox.showwarning("提示", "请先选择游戏窗口")
            return
        hwnd = h[0]
        word, pinyin = "琪里耶", "qiliye"
        win.focus(hwnd)
        time.sleep(0.2)
        kb = Keyboard(key_delay=0.02, send_mode="vk")
        kb.bind(hwnd)
        eng = Engine(self.cfg, self.ocr, self.lex, kb, log=lambda m, t="info": self._log("  " + m, t))

        def read_box():
            return eng.read_input_retry() or ""

        self._log("--- 提交方式测试 (目标: %s / %s) ---" % (word, pinyin), "info")
        self._log("游戏是否前台: %s" % ("是" if win.foreground() == hwnd else "否 ← 会影响结果"),
                  "info")
        for style in ["plain", "space", "digit1", "unicode", "unicode_post"]:
            kb.send_mode = self.cfg.get("working_mode") or "vk"
            eng.clear_input()
            try:
                win.focus(hwnd)
            except Exception:
                pass
            if style == "unicode":
                kb.type_unicode(word)
            elif style == "unicode_post":
                kb.post_unicode(word)
            else:
                kb.type_text(pinyin)
                time.sleep(0.25)
                if style == "space":
                    kb.press("space")
                elif style == "digit1":
                    kb.press("1")
            time.sleep(0.4)
            got = read_box()
            ok = "✅" if got and (got == word or word in got) else "❌"
            self._log("  [%-12s] 框内=%r  %s" % (style, got, ok), "done" if ok else "miss")
            eng.clear_input()
            time.sleep(0.1)
        self._log("哪种出现「琪里耶」，就是游戏认可的输入方式", "info")

    def select_input_region(self):
        RegionSelector(self.root, self._input_region_done)

    def _input_region_done(self, rect):
        self.cfg["input_region"] = list(rect)
        save_cfg(self.cfg)
        self._update_region_label()
        self._log("输入框区域设为 %s" % (rect,), "info")

    def reset_input_region(self):
        self.cfg["input_region"] = None
        save_cfg(self.cfg)
        self._update_region_label()
        self._log("输入框区域已清除", "info")

    def select_region(self):
        RegionSelector(self.root, self._region_done)

    def _region_done(self, rect):
        self.cfg["scan_region"] = list(rect)
        save_cfg(self.cfg)
        self._update_region_label()
        self._log("扫描区域设为 %s, 自动试识别一次..." % (rect,), "info")
        self.root.after(100, self.test_once)

    def reset_region(self):
        self.cfg["scan_region"] = None
        save_cfg(self.cfg)
        self._update_region_label()
        self._log("扫描区域恢复为窗口客户区", "info")

    # ---------------- OCR ----------------
    def ensure_ocr(self):
        if self.ocr is None:
            self.status.set("启动 OCR 服务...")
            self.root.update()
            self.ocr = OcrClient()
            self._log("OCR 服务就绪 %s" % self.ocr.info, "info")
        return self.ocr

    def current_region(self):
        r = self.cfg.get("scan_region")
        if r and len(r) == 4 and r[2] > 0 and r[3] > 0:
            return tuple(int(v) for v in r)
        h = win.find_window(self.cfg.get("window_title", "")) if self.cfg.get("window_title") else None
        if not h:
            return None
        rc = win.client_rect(h[0])
        if not rc:
            return None
        x, y, cw, ch = rc
        top, bot = self.cfg.get("scan_top_ratio", 0.0), self.cfg.get("scan_bottom_ratio", 1.0)
        return (x, y + int(ch * top), cw, int(ch * (bot - top)))

    def test_once(self):
        self._on_window_pick()
        try:
            self.ensure_ocr()
        except Exception as e:
            messagebox.showerror("OCR", "OCR 服务启动失败: %s" % e)
            return
        region = self.current_region()
        if not region:
            messagebox.showwarning("提示", "没找到游戏窗口, 请先选择窗口")
            return
        t0 = time.time()
        res = self.ocr.ocr_names(*region, float(self.scale_var.get()), lexicon=self.lex)
        dt = (time.time() - t0) * 1000
        if not res.get("ok"):
            self._log("OCR 失败: %s" % res.get("err"), "err")
            return
        self.last_result = res
        self._log("试识别 区域=%s  耗时 %.0fms  行数=%d  OCR=%d次  有效倍率=%.1f" %
                  (region, dt, len(res.get("lines", [])), res.get("ocr_passes", 1),
                   self.ocr._starting_name_scale(float(self.scale_var.get()))), "info")
        match_start = time.perf_counter()
        for ln in res.get("lines", []):
            text = norm_text(ln.get("t", ""))
            if not text:
                continue
            m = self.lex.best(text)
            mark = "词库:%s(%s)" % (m.word, m.pinyin) if m else "词库外"
            self._log("  %-14r  %s" % (ln.get("t"), mark), "hit" if m else "warn")
        self._log("词库匹配与日志耗时 %.1fms" % ((time.perf_counter()-match_start)*1000), "info")
        try:
            os.makedirs(os.path.join(BASE, ".cache"), exist_ok=True)
            self.ocr.save_last(os.path.join(BASE, ".cache", "preview.png"))
            self._load_preview()
        except Exception:
            pass

    # ---------------- 引擎 ----------------
    def toggle_engine(self):
        if self.engine and not self.engine.stop_flag:
            self.engine.pause_flag = not self.engine.pause_flag
            self.btn_start.config(text="▶ 继续 (F8)" if self.engine.pause_flag else "⏸ 暂停 (F8)")
            return
        self.start_engine()

    def start_engine(self):
        if self.thread and self.thread.is_alive():
            self._log("上一轮尚未停止，请稍后再启动", "warn")
            return
        if not 0 <= self.win_box.current() < len(self.wins):
            messagebox.showwarning("目标窗口", "请刷新窗口并在下拉框中选择目标，避免沿用已关闭窗口的旧选择")
            return
        self._on_window_pick()
        self.cfg.update({
            "dry_run": self.dry_var.get(),
            "candidate_mode": False,
            "preview": self.prev_var.get(),
            "verify": self.verify_var.get(),
            "ocr_scale": float(self.scale_var.get()),
            "cooldown": float(self.cool_var.get()),
            "submit": self.submit_var.get(),
            "send_mode": self.sendmode_var.get(),
        })
        save_cfg(self.cfg)
        if not self.cfg.get("window_title"):
            messagebox.showwarning("提示", "请先选择游戏窗口")
            return
        try:
            self.ensure_ocr()
        except Exception as e:
            messagebox.showerror("OCR", "OCR 服务启动失败: %s" % e)
            return
        kb = Keyboard(key_delay=self.cfg.get("key_delay", 0.012), send_mode=self.cfg.get("send_mode", "vk"))
        if not self.cfg.get("input_region"):
            self._log("⚠ 还没框选「输入框」：工具无法确认按键是否真的进了游戏，也无法自动清空残留文字。"
                      "强烈建议先点「🖱 框选输入框」。", "warn")
        self.engine = Engine(self.cfg, self.ocr, self.lex, kb,
                             log=lambda msg, tag="info": self.q.put(("log", msg, tag)),
                             on_event=lambda kind, data: self.q.put((kind, data)))
        self.thread = threading.Thread(target=self.engine.run, daemon=True)
        self.thread.start()
        self.btn_start.config(text="⏸ 暂停 (F8)")
        self.status.set("运行中")
        self._paused_shown = False
        # 把自己的窗口最小化, 别挡着游戏(否则截屏会截到自己)
        if self.cfg.get("hide_on_start", False):
            self.root.withdraw()

    def stop_engine(self):
        if self.engine:
            self.engine.stop_flag = True
            self.engine = None
        save_cfg(self.cfg)
        try:
            self.root.deiconify()
            self.root.lift()
        except Exception:
            pass
        self.btn_start.config(text="▶ 启动 (F8)")
        self.status.set("已停止")

    # ---------------- 消息循环 ----------------
    def _tick(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1], item[2])
                elif kind == "preview":
                    self._load_preview()
                elif kind == "frame":
                    self._draw_boxes(item[1])
        except queue.Empty:
            pass
        if self.engine and self.engine.pause_flag and not self._paused_shown:
            self._paused_shown = True
            try:
                self.root.deiconify()
                self.root.lift()
            except Exception:
                pass
            self.status.set("已暂停(看日志)")
        elif not self.engine:
            self._paused_shown = False
        self._hotkeys()
        self.root.after(100, self._tick)

    def _hotkeys(self):
        for vk, name in ((0x77, "F8"), (0x78, "F9")):
            down = win.key_down(vk)
            if down and not self.hotkey_state.get(name):
                if name == "F8":
                    self.toggle_engine()
                else:
                    self.stop_engine()
            self.hotkey_state[name] = down

    def _load_preview(self):
        path = os.path.join(BASE, ".cache", "preview.png")
        if not os.path.exists(path):
            return
        try:
            # PhotoImage 原生读取 OCR 保存的 PNG，无需第三方图像库。
            image = tk.PhotoImage(master=self.root, file=path)
        except (OSError, tk.TclError):
            # 宿主可能尚在写文件，保留上一张有效图，下一帧再读。
            return
        self.prev_img = image
        self._show_img()

    def _draw_boxes(self, data):
        if not self.prev_var.get() or self.prev_img is None:
            return
        try:
            self._show_img()
            factor = self._preview_factor
            for target in data.get("targets", []):
                self.cv.create_rectangle(target.x/factor, target.y/factor,
                                         (target.x+target.w)/factor, (target.y+target.h)/factor,
                                         outline="#00ff78", width=2)
        except tk.TclError:
            pass

    def _show_img(self, img=None):
        image = self.prev_img if img is None else img
        if image is None:
            return
        try:
            width, height = image.width(), image.height()
            canvas_w = max(80, self.cv.winfo_width())
            canvas_h = max(80, self.cv.winfo_height())
            factor = max(1, (width+canvas_w-1)//canvas_w, (height+canvas_h-1)//canvas_h)
            # 保留引用，避免 Tk 图片在 Python 回收后变空。
            self._tkimg = image if factor == 1 else image.subsample(factor, factor)
            self._preview_factor = factor
            self.cv.delete("all")
            self.cv.create_image(0, 0, anchor="nw", image=self._tkimg)
            self.cv.create_text(4, 4, anchor="nw", text="%sx%s" % (width, height), fill="#00ff88")
        except tk.TclError:
            pass

    def _log(self, msg, tag="info"):
        if threading.current_thread() is not threading.main_thread():
            self.q.put(("log", msg, tag))
            return
        ts = time.strftime("%H:%M:%S")
        self.log_txt.insert("end", "[%s] %s\n" % (ts, msg), tag)
        self.log_txt.see("end")
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > 400:
            self.log_txt.delete("1.0", "%d.0" % (lines - 300))

    def on_close(self):
        self.stop_engine()
        if self.ocr:
            try:
                self.ocr.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    win.set_dpi_aware()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
