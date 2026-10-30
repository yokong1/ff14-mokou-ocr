"""最小化窗口枚举回归；只使用模拟 API，不改变真实窗口状态。"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import win

class MinimizedWindowTests(unittest.TestCase):
    def enumerate(self,minimized=False,actual=(30,40,900,600),normal=(300,200,1456,939),visible=True,child=False,placement_ok=True):
        title='魔光键影 OCR 本地测试场'
        api=Mock()
        api.IsWindowVisible.return_value=visible
        api.GetWindowLongW.return_value=win.WS_VISIBLE|(win.WS_CHILD if child else 0)
        api.GetWindowTextLengthW.return_value=len(title)
        def text(hwnd,buffer,count):buffer.value=title;return len(title)
        api.GetWindowTextW.side_effect=text
        def rect(hwnd,pointer):
            r=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
            r.left,r.top,r.right,r.bottom=actual[0],actual[1],actual[0]+actual[2],actual[1]+actual[3];return 1
        api.GetWindowRect.side_effect=rect
        api.IsIconic.return_value=minimized
        def placement(hwnd,pointer):
            p=ctypes.cast(pointer,ctypes.POINTER(win.WINDOWPLACEMENT)).contents
            self.assertEqual(p.length,ctypes.sizeof(win.WINDOWPLACEMENT))
            r=p.rcNormalPosition;r.left,r.top,r.right,r.bottom=normal[0],normal[1],normal[0]+normal[2],normal[1]+normal[3]
            return placement_ok
        api.GetWindowPlacement.side_effect=placement
        api.EnumWindows.side_effect=lambda callback,lparam:callback(123,lparam)
        with patch.object(win,'user32',api):rows=win.list_windows(with_proc=False)
        return rows,api
    def test_real_minimized_size_does_not_hide_full_sized_test_window(self):
        rows,api=self.enumerate(minimized=True,actual=(-32000,-32000,129,28))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0][1],'魔光键影 OCR 本地测试场')
        api.GetWindowPlacement.assert_called_once()
    def test_restored_coordinates_are_not_returned_as_screen_coordinates(self):
        rows,_=self.enumerate(minimized=True,actual=(-32000,-32000,129,28),normal=(10,50,1456,939))
        self.assertEqual(rows[0][3],(-32000,-32000,129,28))
    def test_normal_window_keeps_original_filter_and_no_placement_query(self):
        rows,api=self.enumerate()
        self.assertEqual(rows[0][3],(30,40,900,600))
        api.GetWindowPlacement.assert_not_called()
    def test_small_unminimized_window_is_still_filtered(self):
        rows,_=self.enumerate(actual=(0,0,129,28))
        self.assertEqual(rows,[])
    def test_minimized_window_with_small_normal_size_stays_filtered(self):
        rows,_=self.enumerate(minimized=True,actual=(-32000,-32000,129,28),normal=(0,0,160,80))
        self.assertEqual(rows,[])
    def test_hidden_window_stays_filtered(self):
        rows,_=self.enumerate(minimized=True,visible=False)
        self.assertEqual(rows,[])
    def test_child_window_stays_filtered(self):
        rows,_=self.enumerate(minimized=True,child=True)
        self.assertEqual(rows,[])
    def test_failed_placement_query_preserves_original_actual_size_check(self):
        rows,_=self.enumerate(minimized=True,actual=(-32000,-32000,129,28),placement_ok=False)
        self.assertEqual(rows,[])

if __name__=='__main__':unittest.main()
