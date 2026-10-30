"""Windows 窗口 / 屏幕相关封装 (纯 ctypes, 无第三方依赖)。"""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# ---------------- DPI ----------------
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_aware():
    try:
        user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        return True
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def screen_size():
    return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


# ---------------- 窗口枚举 ----------------
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]

GWL_STYLE = -16
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.POINTER(ctypes.c_ulong)]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL


def list_windows(min_w=320, min_h=240, with_proc=True):
    """返回可见的顶层窗口 [(hwnd, title, proc_path, (x,y,w,h)), ...]"""
    result = []

    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        if not (style & WS_VISIBLE) or (style & WS_CHILD):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, buf, n + 2)
        title = buf.value.strip()
        if not title:
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        filter_w, filter_h = w, h
        if user32.IsIconic(hwnd):
            # 最小化时 GetWindowRect 可能只剩 129×28；用恢复尺寸过滤，不改屏幕坐标。
            placement = WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(WINDOWPLACEMENT)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                normal = placement.rcNormalPosition
                filter_w, filter_h = normal.right - normal.left, normal.bottom - normal.top
        if filter_w < min_w or filter_h < min_h:
            return True
        result.append((hwnd, title, _process_name(hwnd) if with_proc else "", (r.left, r.top, w, h)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return result


def find_window(keyword):
    """按标题/进程名关键字查找窗口 (忽略大小写)。优先返回面积最大的。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    hits = [w for w in list_windows(with_proc=False) if kw in w[1].lower()]
    if not hits:
        hits = [w for w in list_windows(with_proc=True) if kw in w[2].lower()]
    if not hits:
        return None
    hits.sort(key=lambda w: w[3][2] * w[3][3], reverse=True)
    return hits[0]


def client_rect(hwnd):
    """客户区在屏幕上的绝对坐标 (x, y, w, h)"""
    rc = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    return (pt.x, pt.y, w, h)


def is_minimized(hwnd):
    return bool(user32.IsIconic(hwnd))


user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL


def is_window(hwnd):
    return hwnd is not None and bool(user32.IsWindow(hwnd))


def foreground():
    return user32.GetForegroundWindow()


def focus(hwnd):
    """把窗口切到前台; 返回是否成功。"""
    if not hwnd:
        return False
    if foreground() == hwnd:
        return True
    try:
        if is_minimized(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        fg = foreground()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
        attached = []
        for tid in {fg_tid, tgt_tid}:
            if tid and tid != cur_tid:
                if user32.AttachThreadInput(cur_tid, tid, True):
                    attached.append(tid)
        user32.BringWindowToTop(hwnd)
        ok = user32.SetForegroundWindow(hwnd)
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)
        if foreground() != hwnd:
            # 前台锁: 补一个 ALT 键释放, 再试一次
            try:
                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x12, 0, 2, 0)
            except Exception:
                pass
            user32.SetForegroundWindow(hwnd)
        return bool(ok) or foreground() == hwnd
    except Exception:
        return False


user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)
