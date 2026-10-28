"""键盘模拟 (SendInput)。只发送 ASCII 字母 / 数字 / 功能键。"""
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)
LONG = ctypes.c_long
DWORD = wintypes.DWORD
WORD = wintypes.WORD

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004

VK = {
    "enter": 0x0D, "back": 0x08, "esc": 0x1B, "space": 0x20, "tab": 0x09,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22, "del": 0x2E,
    "equal": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    "lbracket": 0xDB, "rbracket": 0xDD, "semicolon": 0xBA, "quote": 0xDE,
    "slash": 0xBF, "backslash": 0xDC, "grave": 0xC0,
    "add": 0x6B, "subtract": 0x6D,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = ord(_c.upper())
for _c in "0123456789":
    VK[_c] = ord(_c)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [DWORD, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = DWORD
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint


class Keyboard:
    """send_mode:
       'vk'   普通文本输入 (SendInput 虚拟键)
       'scan' 扫描码 (SendInput, 部分引擎只认扫描码)
       'post' PostMessage 直投窗口 (绕过前台/权限限制, 很多游戏输入框吃这个)
       'postchar' 只投 WM_CHAR (最"文本化"的一种)
    """

    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_CHAR = 0x0102
    WM_IME_CHAR = 0x0286

    def __init__(self, key_delay=0.008, send_mode="vk"):
        self.key_delay = key_delay
        self.send_mode = send_mode
        self.hwnd = None

    def bind(self, hwnd):
        self.hwnd = hwnd

    # ---------- SendInput ----------
    def _send(self, vk, up=False, extended=False):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        flags = 0
        if up:
            flags |= KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if self.send_mode == "scan":
            inp.ki.wScan = user32.MapVirtualKeyW(vk, 0)
            inp.ki.wVk = 0
            flags |= KEYEVENTF_SCANCODE
        else:
            inp.ki.wVk = vk
            inp.ki.wScan = user32.MapVirtualKeyW(vk, 0)
        inp.ki.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def tap(self, vk, extended=False):
        self._send(vk, up=False, extended=extended)
        if self.key_delay:
            time.sleep(self.key_delay * 0.5)
        self._send(vk, up=True, extended=extended)

    def type_text(self, text):
        """逐字符敲入 ASCII 文本"""
        if self.send_mode in ("post", "postchar"):
            return self._post_text(text)
        for ch in text:
            low = ch.lower()
            if low not in VK:
                continue
            vk = VK[low]
            self._send(vk, up=False)
            if self.key_delay:
                time.sleep(self.key_delay * 0.5)
            self._send(vk, up=True)
            if self.key_delay:
                time.sleep(self.key_delay)

    # ---------- PostMessage ----------
    def _post(self, msg, wparam, lparam):
        if not self.hwnd:
            return False
        return bool(user32.PostMessageW(self.hwnd, msg, wparam, lparam))

    def _post_key(self, vk, char=None):
        scan = user32.MapVirtualKeyW(vk, 0)
        lp = 1 | (scan << 16)
        if self.send_mode == "post":
            self._post(self.WM_KEYDOWN, vk, lp)
        if char is not None:
            self._post(self.WM_CHAR, ord(char), lp)
        elif self.send_mode == "post":
            self._post(self.WM_CHAR, vk, lp)
        if self.send_mode == "post":
            self._post(self.WM_KEYUP, vk, lp | (1 << 30) | (1 << 31))

    def _post_text(self, text):
        for ch in text:
            low = ch.lower()
            vk = VK.get(low)
            if vk is None:
                continue
            self._post_key(vk, low)
            if self.key_delay:
                time.sleep(self.key_delay)

    # ---------- 直接输入任意 Unicode 字符 (跳过输入法) ----------
    def _send_unicode(self, code, down):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = 0
        inp.ki.wScan = code & 0xFFFF
        inp.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if down else 0)
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def type_unicode(self, text):
        """用 SendInput 的 Unicode 模式把汉字直接打进去(不经过输入法)"""
        for ch in text:
            self._send_unicode(ord(ch), down=True)
            self._send_unicode(ord(ch), down=False)
            if self.key_delay:
                time.sleep(self.key_delay)

    def post_unicode(self, text, ime_char=False):
        """用 PostMessage 把汉字直接投给窗口"""
        if not self.hwnd:
            return False
        msg = self.WM_IME_CHAR if ime_char else self.WM_CHAR
        for ch in text:
            self._post(msg, ord(ch), 1)
            if self.key_delay:
                time.sleep(self.key_delay)
        return True

    def clear_pending(self):
        pass

    def enter(self):
        if self.send_mode in ("post", "postchar"):
            self._post_key(VK["enter"], "\r")
            return
        self.tap(VK["enter"])

    # ---------- 回车: 三种独立通路, 由调用方逐个试 ----------
    def enter_via(self, mode):
        vk = VK["enter"]
        if mode == "vk":
            self.tap(vk)                       # SendInput 虚拟键
        elif mode == "scan":
            self._send(vk, up=False)
            self._send(vk, up=True)
        elif mode == "post":
            scan = user32.MapVirtualKeyW(vk, 0)
            lp = 1 | (scan << 16)
            self._post(self.WM_KEYDOWN, vk, lp)
            self._post(self.WM_KEYUP, vk, lp | (1 << 30) | (1 << 31))
        elif mode == "postchar":
            scan = user32.MapVirtualKeyW(vk, 0)
            self._post(self.WM_CHAR, ord("\r"), 1 | (scan << 16))
        elif mode == "ime":
            scan = user32.MapVirtualKeyW(vk, 0)
            self._post(self.WM_IME_CHAR, ord("\r"), 1 | (scan << 16))

    def backspace(self, n=1):
        for _ in range(n):
            if self.send_mode in ("post", "postchar"):
                self._post_key(VK["back"], "\b")
            else:
                self.tap(VK["back"])

    def press(self, name):
        vk = VK.get(str(name).lower())
        if vk is None:
            return False
        if self.send_mode in ("post", "postchar"):
            self._post_key(vk, None)
            return True
        self.tap(vk)
        return True
