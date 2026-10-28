"""OCR 服务客户端: 管理 bin/ocr_host.exe 常驻进程, 使用匿名管道逐行通信。"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "bin", "ocr_host.exe")


class OcrClient:
    def __init__(self, exe=EXE):
        self.proc = subprocess.Popen(
            [exe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        info_line = self.proc.stdout.readline().decode("utf-8", "replace")
        try:
            self.info = json.loads(info_line)
        except Exception:
            raise RuntimeError("ocr_host 启动失败: %r" % info_line)
        if not self.info.get("ok"):
            raise RuntimeError("ocr_host 初始化失败: %s" % self.info)

    def _call(self, line):
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            raise RuntimeError("ocr_host 已退出")
        return json.loads(raw.decode("utf-8", "replace"))

    def ocr_region(self, x, y, w, h, scale=2.0):
        return self._call("OCR %d %d %d %d %.3f" % (x, y, w, h, scale))

    def ocr_file(self, path, scale=2.0):
        return self._call("FILE %s %.3f" % (path, scale))

    def save_last(self, path):
        return self._call("SAVE %s" % path)

    def close(self):
        try:
            self.proc.stdin.write(b"QUIT\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()
