"""0.9.1：区域提示、长词与彩色补试。全部使用模拟窗口和键盘。"""
import ast
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
from core.engine import Engine
from core.lookup import Lexicon,norm_text
from core.ocrclient import OcrClient


def line(text,x=10,y=20,w=100,h=25):return dict(t=text,x=x,y=y,w=w,h=h,words=[])

class ColorRetryTests(unittest.TestCase):
    def setUp(self):
        self.client=OcrClient.__new__(OcrClient)
        self.client._call=Mock()
        self.color_response=dict(ok=False,lines=[])
        self.client._call.side_effect=lambda command: self.color_response if command=='COLOR' else dict(ok=False,lines=[])
        self.lex=Mock(words={'原型猎敌者':'','空中兵':''})
    def test_clean_original_does_not_add_a_second_ocr(self):
        raw=dict(ok=True,lines=[line('原型猎敌者')])
        self.assertIs(self.client._retry_names(raw,self.lex),raw)
        self.client._call.assert_not_called()
    def test_partial_yellow_word_can_be_recovered(self):
        self.color_response=dict(ok=True,padding=16,lines=[line('原型猎敌者',26,36)])
        raw=dict(ok=True,img=[400,100],lines=[line('原型犭')])
        fixed=self.client._retry_names(raw,self.lex)
        self.assertEqual([norm_text(l['t']) for l in fixed['lines']],['原型猎敌者'])
        self.assertEqual(fixed['lines'][0]['x'],10)
        self.assertEqual(fixed['lines'][0]['y'],20)
        self.assertEqual(fixed['img'],[400,100])
    def test_color_retry_accepts_only_exact_lexicon_words(self):
        self.color_response=dict(ok=True,padding=16,lines=[line('某个未知内容',26,36)])
        raw=dict(ok=True,lines=[line('原型犭')])
        self.assertIs(self.client._retry_names(raw,self.lex),raw)
    def test_retry_does_not_replace_already_reliable_original(self):
        self.color_response=dict(ok=True,padding=16,lines=[line('原型猎敌者',26,36),line('空中兵',250,36)])
        raw=dict(ok=True,lines=[line('原型猎敌者'),line('空中宾',234,20)])
        fixed=self.client._retry_names(raw,self.lex)
        self.assertEqual(len(fixed['lines']),2)
        self.assertEqual(sum(norm_text(l['t'])=='原型猎敌者' for l in fixed['lines']),1)
        self.assertEqual(fixed['color_recovered'],1)
    def test_failed_color_pass_preserves_original(self):
        self.color_response=dict(ok=False,err='no image')
        raw=dict(ok=True,lines=[line('原型犭')])
        self.assertIs(self.client._retry_names(raw,self.lex),raw)
    def test_error_from_original_is_not_hidden(self):
        raw=dict(ok=False,lines=[])
        self.assertIs(self.client._retry_names(raw,self.lex),raw)
        self.client._call.assert_not_called()
    def test_overlapping_word_boxes_are_not_duplicated(self):
        self.assertTrue(self.client._same_box(line('a'),line('b',12,21)))
        self.assertFalse(self.client._same_box(line('a'),line('b',300,200)))

class LongBossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.lex=Lexicon(ROOT/'词条总表.tsv')
    def test_original_twelve_char_config_cannot_exclude_long_lexicon_words(self):
        words=[word for word in self.lex.words if len(word)>12]
        self.assertTrue(words)
        engine=Engine(dict(app.DEFAULT_CFG,max_len=12,blacklist=[]),Mock(),self.lex,Mock())
        engine.skip_rects=lambda:[]
        targets=engine.parse(dict(ok=True,lines=[line(word,y=i*30) for i,word in enumerate(words)]))
        self.assertEqual({t.text for t in targets},set(words))
    def test_oversized_nonlexicon_noise_remains_filtered(self):
        engine=Engine(dict(app.DEFAULT_CFG,max_len=12,blacklist=[]),Mock(),self.lex,Mock())
        engine.skip_rects=lambda:[]
        self.assertEqual(engine.parse(dict(ok=True,lines=[line('很长的无关文字'*20)])),[])
    def test_default_covers_current_lexicon(self):
        self.assertGreaterEqual(app.DEFAULT_CFG['max_len'],max(map(len,self.lex.words)))

class TooltipAndUiTests(unittest.TestCase):
    def test_name_version_and_removed_buttons(self):
        self.assertEqual(app.APP_NAME,'ff14 mokou ocr')
        self.assertEqual(app.APP_VERSION,'0.9.2')
        tree=ast.parse((ROOT/'app.py').read_text(encoding='utf-8'))
        ui=next(m for c in tree.body if isinstance(c,ast.ClassDef) and c.name=='App' for m in c.body if isinstance(m,ast.FunctionDef) and m.name=='_build_ui')
        text=ast.get_source_segment((ROOT/'app.py').read_text(encoding='utf-8'),ui)
        for token in ('测试按键','测提交方式','存截图','command=self.test_typing','command=self.test_submit','command=self.snap'):
            self.assertNotIn(token,text)
        self.assertIn('ToolTip(self.region_label, SCAN_REGION_HELP)',text)
        self.assertIn('ToolTip(self.input_region_label, INPUT_REGION_HELP)',text)
    def test_help_text_explains_both_region_boundaries(self):
        self.assertIn('怪物名字',app.SCAN_REGION_HELP)
        self.assertIn('不要框取多余',app.SCAN_REGION_HELP)
        self.assertIn('文本输入框',app.INPUT_REGION_HELP)
        self.assertIn('左侧输入法',app.INPUT_REGION_HELP)
        self.assertIn('中',app.INPUT_REGION_HELP)
    def test_tooltip_schedules_and_cancels_without_changing_input(self):
        widget=Mock();widget.after.return_value='hover-id'
        tip=app.ToolTip(widget,'说明')
        bound=[c.args[0] for c in widget.bind.call_args_list]
        self.assertEqual(bound,['<Enter>','<Leave>','<ButtonPress>','<Destroy>'])
        tip._schedule();self.assertEqual(tip.after_id,'hover-id')
        tip._hide();widget.after_cancel.assert_called_once_with('hover-id')
        self.assertIsNone(tip.tip)
        widget.focus_set.assert_not_called()
    def test_tooltip_show_and_leave_use_only_fake_tk_widgets(self):
        widget=Mock();widget.winfo_pointerx.return_value=200;widget.winfo_pointery.return_value=100
        tip=app.ToolTip(widget,'说明')
        with patch.object(app.tk,'Toplevel') as window,patch.object(app.tk,'Label') as label:
            tip._show();window.assert_called_once_with(widget)
            label.assert_called_once();tip._hide();window.return_value.destroy.assert_called_once()
            window.return_value.focus_force.assert_not_called()

if __name__=='__main__':unittest.main()
