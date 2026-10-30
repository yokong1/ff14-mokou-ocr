"""原版基线的小范围回归。不会创建窗口、读屏、聚焦或发送真实输入。"""
import ast
import copy
import ctypes
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
from core.engine import Engine,Target
from core.keys import Keyboard,INPUT,KEYEVENTF_UNICODE,KEYEVENTF_KEYUP
from core.lookup import Match

class FakeBox:
    def __init__(self,selected=-1):self.selected=selected;self.values=[]
    def __setitem__(self,key,value):self.values=value
    def set(self,text):self.selected=-1
    def current(self,index=None):
        if index is None:return self.selected
        self.selected=index

class WindowSelectionTests(unittest.TestCase):
    def ui(self,title='',selected=-1):
        ui=app.App.__new__(app.App);ui.cfg={'window_title':title};ui.win_box=FakeBox(selected)
        ui._update_region_label=Mock();ui._on_window_pick=Mock();return ui
    def windows(self):
        return [(1,'文件资源管理器',r'C:\Windows\explorer.exe',(0,0,900,800)),
                (2,'魔光键影 OCR 本地测试场 · 4 次换词 · 30 秒无输入结束',r'D:\AI\识别\测试程序\MokouOcrSimulator.exe',(0,0,1456,939)),
                (3,'识别打怪助手 自定义窗口',r'C:\python.exe',(0,0,980,740))]
    def test_lists_test_exe_and_other_visible_windows_without_new_filters(self):
        ui=self.ui('魔光键影 OCR 本地测试场');rows=self.windows()
        with patch.object(app.win,'list_windows',return_value=rows):ui.refresh_windows()
        self.assertEqual(ui.wins,rows);self.assertEqual(len(ui.win_box.values),3)
        self.assertEqual(ui.win_box.current(),1)
    def test_refresh_keeps_target_by_title_after_list_order_changes(self):
        ui=self.ui('魔光键影 OCR 本地测试场',selected=0)
        with patch.object(app.win,'list_windows',return_value=self.windows()):ui.refresh_windows()
        self.assertEqual(ui.win_box.current(),1)
        ui._on_window_pick.assert_not_called()
    def test_missing_target_clears_old_index_instead_of_selecting_explorer(self):
        ui=self.ui('刚关闭的游戏窗口',selected=0)
        with patch.object(app.win,'list_windows',return_value=self.windows()):ui.refresh_windows()
        self.assertEqual(ui.win_box.current(),-1);self.assertEqual(ui.cfg['window_title'],'刚关闭的游戏窗口')
        ui._on_window_pick.assert_not_called()
    def test_original_case_insensitive_title_keyword_match_is_preserved(self):
        ui=self.ui('my GAME')
        with patch.object(app.win,'list_windows',return_value=[(4,'My Game · 运行中','game.exe',(0,0,800,600))]):ui.refresh_windows()
        self.assertEqual(ui.win_box.current(),0)
    def test_no_saved_title_uses_original_first_window_behavior(self):
        ui=self.ui()
        with patch.object(app.win,'list_windows',return_value=self.windows()):ui.refresh_windows()
        self.assertEqual(ui.win_box.current(),0);ui._on_window_pick.assert_called_once()
    def test_start_requires_explicit_valid_selection(self):
        ui=self.ui('旧目标');ui.wins=self.windows();ui.thread=None
        with patch.object(app.messagebox,'showwarning') as warning:ui.start_engine()
        warning.assert_called_once();ui._on_window_pick.assert_not_called()
    def test_old_thread_blocks_overlapping_restart(self):
        ui=self.ui();ui.thread=Mock();ui.thread.is_alive.return_value=True;ui._log=Mock()
        ui.start_engine();ui._log.assert_called_once();ui._on_window_pick.assert_not_called()

class OriginalInputBehaviorTests(unittest.TestCase):
    def engine(self):
        cfg=copy.deepcopy(app.DEFAULT_CFG);cfg.update(window_title='测试',input_region=[0,0,800,50])
        ocr,kb,lex=Mock(),Mock(),Mock();eng=Engine(cfg,ocr,lex,kb);return eng,kb,ocr
    def test_original_ocr_typo_is_accepted_without_pause(self):
        eng,kb,ocr=self.engine();eng.clear_input=Mock();eng.read_input_retry=Mock(return_value='空中宾')
        with patch('core.engine.time.sleep'):
            used=eng.type_unicode_probe('空中兵',10)
        self.assertEqual(used,'unicode');kb.type_unicode.assert_called_once_with('空中兵');self.assertFalse(eng.pause_flag)
    def test_original_different_nonempty_read_is_advisory(self):
        eng,kb,ocr=self.engine();eng.clear_input=Mock();eng.read_input_retry=Mock(return_value='不同内容')
        with patch('core.engine.time.sleep'):used=eng.type_unicode_probe('空中兵',10)
        self.assertEqual(used,'unicode?');self.assertFalse(eng.pause_flag)
    def test_original_send_checks_clear_input_not_character_equality(self):
        eng,kb,ocr=self.engine();eng.read_input=Mock(return_value='')
        with patch('core.engine.time.sleep'):self.assertEqual(eng.send_message(),1)
        kb.enter_via.assert_called_once_with('vk')
    def test_empty_scaled_input_uses_native_size_fallback(self):
        eng,kb,ocr=self.engine()
        ocr.ocr_region.side_effect=[dict(ok=True,lines=[]),dict(ok=True,lines=[dict(t='加')])]
        self.assertEqual(eng.read_input(),'加');self.assertEqual(ocr.ocr_region.call_args_list[-1].args[-1],1.)
    def test_readable_scaled_input_does_not_add_an_extra_ocr_call(self):
        eng,kb,ocr=self.engine();ocr.ocr_region.return_value=dict(ok=True,lines=[dict(t='空 中 兵')])
        self.assertEqual(eng.read_input(),'空中兵');self.assertEqual(ocr.ocr_region.call_count,1)
    def test_original_ocr_error_remains_unknown(self):
        eng,kb,ocr=self.engine();ocr.ocr_region.return_value=dict(ok=False,lines=[])
        self.assertIsNone(eng.read_input());self.assertEqual(ocr.ocr_region.call_count,1)
    def test_unicode_flag_fix_retains_original_sendinput_path(self):
        recorded=[]
        def send(n,pointer,size):
            key=ctypes.cast(pointer,ctypes.POINTER(INPUT)).contents.ki;recorded.append((key.wScan,key.dwFlags));return 1
        with patch('core.keys.user32') as user:
            user.SendInput.side_effect=send;Keyboard(key_delay=0).type_unicode('兵')
        self.assertEqual(recorded,[(ord('兵'),KEYEVENTF_UNICODE),(ord('兵'),KEYEVENTF_UNICODE|KEYEVENTF_KEYUP)])
    def test_original_full_enter_unicode_enter_path(self):
        eng,kb,ocr=self.engine();eng.cfg.update(verify=False,submit_styles=['unicode'],working_submit='unicode')
        eng.hwnd=lambda:10;eng.next_attempt=lambda text:('空中兵','kong zhong bing');eng.plan_for=lambda text:[('空中兵','kong zhong bing')]*4
        eng.clear_input=Mock();eng.read_input_retry=Mock(return_value='空中宾');eng.read_input=Mock(return_value='')
        events=[];kb.enter.side_effect=lambda:events.append('Enter-open')
        kb.type_unicode.side_effect=lambda text:events.append('Unicode:'+text)
        kb.enter_via.side_effect=lambda mode:events.append('Enter-send')
        with patch('core.engine.win.foreground',return_value=10),patch('core.engine.time.sleep'):
            eng.act(Target('空中兵',Match('空中兵','kong zhong bing',1,True),(10,10,90,22)),(0,0,800,600))
        self.assertEqual(events,['Enter-open','Unicode:空中兵','Enter-send']);self.assertFalse(eng.pause_flag)

class BaselineStructureTests(unittest.TestCase):
    def test_main_entry_is_present(self):
        tree=ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
        self.assertTrue(any(isinstance(n,ast.FunctionDef) and n.name=='main' for n in tree.body))
        self.assertTrue(any(isinstance(n,ast.If) and any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id=='main' for c in ast.walk(n)) for n in tree.body))
    def test_removed_candidate_buttons_have_no_dangling_callback(self):
        source=(ROOT/'app.py').read_text(encoding='utf-8')
        for text in ('command=self.select_cand_region','command=self.probe_candidates','self.cand_var','self.pagekey_var'):
            self.assertNotIn(text,source)
    def test_legacy_config_can_load_without_v9_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json';path.write_text(json.dumps({'config_version':8,'window_title':'测试','candidate_mode':True}),encoding='utf-8')
            with patch.object(app,'CONFIG_PATH',str(path)):cfg=app.load_cfg()
            self.assertEqual(cfg['window_title'],'测试');self.assertFalse(cfg['candidate_mode'])
    def test_v9_strict_workflow_is_absent(self):
        source=inspect.getsource(Engine)
        for text in ('def confirm_targets','def _input_guard','def verify_target','def read_input_result','window_hwnd'):
            self.assertNotIn(text,source)

if __name__=='__main__':unittest.main()
