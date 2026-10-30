"""OCR 服务客户端: 管理 bin/ocr_host.exe 常驻进程, 使用匿名管道逐行通信。"""
import json
import os
import subprocess
import sys
import threading
import time

from .lookup import norm_text, has_latin

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "bin", "ocr_host_0_9_2.exe")


class OcrClient:
    def __init__(self, exe=EXE):
        self._lock = threading.RLock()
        self._name_scale_hint = None
        self._next_name_audit = 0.0
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
        with self._lock:
            return self._call_locked(line)

    def _call_locked(self, line):
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            raise RuntimeError("ocr_host 已退出")
        return json.loads(raw.decode("utf-8", "replace"))

    def ocr_region(self, x, y, w, h, scale=2.0):
        result = self._call("OCR %d %d %d %d %.3f" % (x, y, w, h, scale))
        result["region"] = (x, y, w, h)
        return result

    def ocr_file(self, path, scale=2.0):
        return self._call("FILE\t%s\t%.3f" % (path, scale))

    def _starting_name_scale(self, requested):
        hint = getattr(self, "_name_scale_hint", None)
        return hint[1] if hint and hint[0] == requested else requested

    def _starting_name_padding(self, requested):
        hint = getattr(self, "_name_scale_hint", None)
        return hint[2] if hint and hint[0] == requested and len(hint) > 2 else 0

    def _starting_name_strategy(self, requested):
        hint = getattr(self, "_name_scale_hint", None)
        return hint[3] if hint and hint[0] == requested and len(hint)>3 else "original"

    def _audit_names_now(self):
        now = time.monotonic()
        if now < getattr(self, "_next_name_audit", 0):
            return False
        self._next_name_audit = now+.6
        return True

    @staticmethod
    def _remove_padding(result):
        padding = int(result.get("padding", 0))
        if not result.get("ok") or not padding:
            return result
        lines = []
        for line in result.get("lines", []):
            words = [dict(word, x=word["x"]-padding, y=word["y"]-padding) for word in line.get("words", [])]
            lines.append(dict(line, x=line["x"]-padding, y=line["y"]-padding, words=words))
        return dict(result, lines=lines, img=result.get("source_img", result.get("img")), padding=0)

    def ocr_names(self, x, y, w, h, scale=1.0, lexicon=None):
        """只截图一次；名字漏读时在同一帧补试倍率及对比度。"""
        with self._lock:
            started = time.perf_counter()
            effective = self._starting_name_scale(scale)
            strategy = self._starting_name_strategy(scale)
            prefix = {"contrast":"OCRC", "color":"OCRW"}.get(strategy)
            if prefix or self._starting_name_padding(scale):
                prefix = prefix or "OCRP"
                result = self._remove_padding(self._call("%s %d %d %d %d %.3f" % (prefix,x,y,w,h,effective)))
                result["region"] = (x,y,w,h)
            else:
                result = self.ocr_region(x, y, w, h, effective)
            result = self._retry_names(result, lexicon, effective, scale, self._audit_names_now())
            result["pipeline_ms"] = round((time.perf_counter()-started)*1000, 2)
            return result

    def ocr_names_file(self, path, scale=1.0, lexicon=None):
        """与实时同一策略的离线重放，不截屏、不操作窗口。"""
        with self._lock:
            started = time.perf_counter()
            effective = self._starting_name_scale(scale)
            strategy = self._starting_name_strategy(scale)
            prefix = {"contrast":"FILEC", "color":"FILEW"}.get(strategy)
            if prefix or self._starting_name_padding(scale):
                primary = self._remove_padding(self._call("%s\t%s\t%.3f" % (prefix or "FILEP",path,effective)))
            else:
                primary = self.ocr_file(path, effective)
            result = self._retry_names(primary, lexicon, effective, scale, self._audit_names_now())
            result["pipeline_ms"] = round((time.perf_counter()-started)*1000, 2)
            return result

    @staticmethod
    def _complete_names(lines, lexicon):
        names = [norm_text(line.get("t", "")) for line in lines]
        names = [text for text in names if len(text) >= 2 and not has_latin(text)]
        return bool(names) and all(text in lexicon.words for text in names)

    def _merge_exact_names(self, result, retry, lexicon):
        lines = list(result.get("lines", []))
        padding = int(retry.get("padding", 0))
        recovered = 0
        for candidate in retry.get("lines", []):
            if norm_text(candidate.get("t", "")) not in lexicon.words:
                continue
            candidate = dict(candidate, x=candidate["x"]-padding, y=candidate["y"]-padding)
            candidate["words"] = [dict(word, x=word["x"]-padding, y=word["y"]-padding)
                                  for word in candidate.get("words", [])]
            overlaps = [i for i, old in enumerate(lines) if self._same_box(old, candidate)]
            if any(norm_text(lines[i].get("t", "")) in lexicon.words for i in overlaps):
                continue
            for index in reversed(overlaps):
                lines.pop(index)
            lines.append(candidate)
            recovered += 1
        if not recovered:
            return result, 0
        lines.sort(key=lambda line: (line["y"], line["x"]))
        return dict(result, lines=lines), recovered

    def _retry_names(self, result, lexicon, effective=None, requested=None, audit=False):
        if not result.get("ok") or lexicon is None:
            return result
        complete = self._complete_names(result.get("lines", []), lexicon)
        if complete and not audit:
            result["ocr_passes"] = 1
            return result
        effective = float(effective if effective is not None else result.get("scale", 1.0))
        requested = effective if requested is None else requested
        alternate = 2.0 if effective < 1.5 else 1.0
        plans = [("scale", "RETRY %.3f" % alternate), ("color", "COLOR"),
                 ("contrast", "CONTRAST 2.000")]
        if complete:
            if self._starting_name_strategy(requested) == "contrast":
                plans = [("scale", "RETRY %.3f" % alternate)]
            else:
                plans = [("contrast", "CONTRAST 2.000")]
        passes, strategies = 1, []
        for strategy, command in plans:
            retry = self._call(command)
            passes += 1
            if not retry.get("ok"):
                continue
            result, recovered = self._merge_exact_names(result, retry, lexicon)
            if recovered:
                strategies.append(strategy)
                result[strategy+"_recovered"] = recovered
                if strategy == "scale":
                    # 只记住本轮更有效的倍率；用户改倍率后不会沿用旧提示。
                    self._name_scale_hint = (requested, alternate, int(retry.get("padding", 0)))
                elif strategy == "contrast":
                    self._name_scale_hint = (requested, 2.0, int(retry.get("padding",0)), "contrast")
                elif strategy == "color":
                    self._name_scale_hint = (requested, 1.0, int(retry.get("padding",0)), "color")
                if self._complete_names(result.get("lines", []), lexicon):
                    break
        result["ocr_passes"] = passes
        result["recovery"] = strategies
        return result

    @staticmethod
    def _same_box(first, second):
        width = max(0, min(first["x"]+first["w"], second["x"]+second["w"])-max(first["x"], second["x"]))
        height = max(0, min(first["y"]+first["h"], second["y"]+second["h"])-max(first["y"], second["y"]))
        area = min(first["w"]*first["h"], second["w"]*second["h"])
        return area > 0 and width*height/area >= .35

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
