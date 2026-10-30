"""0.9.2：同帧补试、坐标与重复开销；不创建窗口、读屏或发送真实按键。"""
import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
from core.engine import Engine,Target
from core.lookup import Lexicon,Match
from core.ocrclient import OcrClient


def line(text,x=10,y=10,w=60,h=22):return dict(t=text,x=x,y=y,w=w,h=h,words=[])
def frame(*texts,**extra):return dict(ok=True,scale=1.,img=[600,140],lines=[line(t,x=10+i*100) for i,t in enumerate(texts)],**extra)

class SameFrameRetryTests(unittest.TestCase):
    def setUp(self):
        self.client=OcrClient.__new__(OcrClient)
        self.client._lock=threading.RLock()
        self.client._name_scale_hint=None
        self.client._next_name_audit=float("inf")
        self.client._call=Mock(return_value=dict(ok=False,lines=[]))
        self.lex=Mock(words={'伊莉娜':'','可琳':'','索奇':'','臭气':''})
    def test_zero_lines_are_recovered_at_second_scale_without_another_capture(self):
        retry=frame('伊莉娜','可琳','索奇','臭气');retry.update(padding=12)
        for l in retry['lines']:l.update(x=l['x']+12,y=l['y']+12)
        self.client._call.side_effect=lambda cmd:retry if cmd=='RETRY 2.000' else dict(ok=False,lines=[])
        raw=frame();result=self.client._retry_names(raw,self.lex,1.,1.)
        self.assertEqual({l['t'] for l in result['lines']},set(self.lex.words))
        self.client._call.assert_called_once_with('RETRY 2.000')
        self.assertEqual(result['ocr_passes'],2)
        self.assertEqual(self.client._name_scale_hint,(1.,2.,12))
        self.assertEqual(result['lines'][0]['x'],10)
    def test_next_frame_reuses_scale_and_padding_in_one_request(self):
        self.client._name_scale_hint=(1.,2.,12)
        raw=frame('可琳');raw.update(padding=12,source_img=[90,50]);raw['lines'][0].update(x=22,y=22)
        self.client._call.return_value=raw
        result=self.client.ocr_names_file('saved-image.png',1.,self.lex)
        self.client._call.assert_called_once_with('FILEP\tsaved-image.png\t2.000')
        self.assertEqual(result['lines'][0]['x'],10)
        self.assertEqual(result['img'],[90,50])
        self.assertEqual(result['ocr_passes'],1)
    def test_user_selected_scale_change_does_not_reuse_old_hint(self):
        self.client._name_scale_hint=(1.,2.,12)
        self.assertEqual(self.client._starting_name_scale(1.5),1.5)
        self.assertEqual(self.client._starting_name_padding(1.5),0)
    def test_exact_original_does_not_trigger_retries(self):
        raw=frame('伊莉娜','索奇');self.assertIs(self.client._retry_names(raw,self.lex),raw)
        self.client._call.assert_not_called()
    def test_partial_retry_must_be_exact_lexicon_not_fuzzy_guess(self):
        self.client._call.return_value=frame('可')
        result=self.client._retry_names(frame(),self.lex)
        self.assertEqual(result['lines'],[])
        self.assertEqual(self.client._call.call_count,3)
    def test_original_reliable_text_is_preserved_when_retry_disagrees(self):
        raw=frame('伊莉娜','未知文本');retry=frame('索奇');retry['lines'][0].update(x=10,y=10)
        merged,count=self.client._merge_exact_names(raw,retry,self.lex)
        self.assertEqual(count,0);self.assertIs(merged,raw)
    def test_contrast_is_a_final_bounded_fallback(self):
        retry=frame('可琳');retry.update(padding=16);retry['lines'][0].update(x=26,y=26)
        self.client._call.side_effect=lambda cmd:retry if cmd=='CONTRAST 2.000' else dict(ok=False,lines=[])
        result=self.client._retry_names(frame(),self.lex)
        self.assertEqual(result['lines'][0]['t'],'可琳')
        self.assertEqual(result['lines'][0]['x'],10)
        self.assertEqual(result['ocr_passes'],4)
    def test_audit_finds_missing_words_even_when_one_original_word_is_correct(self):
        retry=frame('伊莉娜','可琳','索奇','臭气')
        self.client._call.side_effect=lambda cmd:retry if cmd=='CONTRAST 2.000' else dict(ok=False,lines=[])
        result=self.client._retry_names(frame('伊莉娜'),self.lex,1.,1.,audit=True)
        self.assertEqual({l['t'] for l in result['lines']},set(self.lex.words))
        self.assertEqual(self.client._name_scale_hint,(1.,2.,0,'contrast'))
    def test_contrast_that_recovers_more_targets_is_used_directly_next_frame(self):
        self.client._name_scale_hint=(1.,2.,16,'contrast')
        prepared=frame('可琳');prepared.update(padding=16,source_img=[90,50]);prepared['lines'][0].update(x=26,y=26)
        self.client._call.return_value=prepared
        result=self.client.ocr_names_file('saved.png',1.,self.lex)
        self.client._call.assert_called_once_with('FILEC\tsaved.png\t2.000')
        self.assertEqual(result['lines'][0]['x'],10)
    def test_audit_has_a_time_interval_and_does_not_run_each_frame(self):
        self.client._next_name_audit=0
        with patch('core.ocrclient.time.monotonic',side_effect=[10.,10.1,10.7]):
            self.assertTrue(self.client._audit_names_now())
            self.assertFalse(self.client._audit_names_now())
            self.assertTrue(self.client._audit_names_now())

    def test_original_ocr_error_does_not_trigger_empty_result_guess(self):
        raw=dict(ok=False,err='service error',lines=[])
        self.assertIs(self.client._retry_names(raw,self.lex),raw)
        self.client._call.assert_not_called()
    def test_file_paths_with_spaces_are_one_protocol_argument(self):
        path=r'C:\用户\图 像.png'
        self.client.ocr_file(path,2.)
        self.client._call.assert_called_once_with('FILE\t'+path+'\t2.000')
    def test_input_read_uses_configured_scale_not_name_scale_hint(self):
        self.client._name_scale_hint=(1.,2.,12)
        self.client._call.return_value=frame('输入')
        self.client.ocr_region(1,2,300,40,1.)
        self.client._call.assert_called_once_with('OCR 1 2 300 40 1.000')

class ScanCoordinatesTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(app.DEFAULT_CFG)
        self.cfg.update(scan_region=[392,302,1153,676],input_region=[628,916,722,41],preview=False,window_title='')
        self.lex=Mock(words={'伊莉娜':'yi li na'})
        self.lex.best.return_value=Match('伊莉娜','yi li na',1,True)
        self.engine=Engine(self.cfg,Mock(),self.lex,Mock())
        self.engine.region=lambda:tuple(self.cfg['scan_region'])
    def test_input_mask_compares_screen_coordinates(self):
        result=frame('伊莉娜');result['region']=(392,302,1153,676)
        result['lines'][0].update(x=240,y=615,w=100,h=22)
        self.assertEqual(self.engine.parse(result),[])
    def test_target_coordinates_stay_local_for_preview(self):
        result=frame('伊莉娜');result['region']=(392,302,1153,676)
        targets=self.engine.parse(result)
        self.assertEqual((targets[0].x,targets[0].y),(10,10))
    def test_main_loop_passes_absolute_coordinates_to_narrow(self):
        self.engine.hwnd=lambda:10
        self.engine.ocr.ocr_names.return_value=frame('伊莉娜')
        self.engine.narrow=Mock()
        def act(*args):self.engine.stop_flag=True
        self.engine.act=Mock(side_effect=act)
        with patch('core.engine.win.is_minimized',return_value=False),patch('core.engine.time.sleep'):
            self.engine.run()
        self.engine.narrow.assert_called_once_with((402,312,60,22))
    def test_repaired_narrow_crop_is_inside_the_full_region(self):
        self.engine.narrow((900,600,80,30))
        with patch('core.engine._now',return_value=self.engine._tight_ttl-.1):
            self.engine._full_scan_ts=self.engine._tight_ttl-.2
            cropped=self.engine.scan_rect()
        self.assertEqual(cropped,(874,574,132,82))
    def test_periodic_full_scan_cannot_be_postponed_by_recropping(self):
        self.engine._tight=(500,400,100,80)
        self.engine._tight_ttl=100
        self.engine._full_scan_ts=5
        with patch('core.engine._now',return_value=5.2):self.assertEqual(self.engine.scan_rect(),self.engine._tight)
        with patch('core.engine._now',return_value=5.7):self.assertEqual(self.engine.scan_rect(),tuple(self.cfg['scan_region']))
    def test_no_region_metadata_preserves_original_local_parsing(self):
        self.engine.cfg['input_region']=None
        self.assertEqual(len(self.engine.parse(frame('伊莉娜'))),1)

class MatchingAndReadReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.lex=Lexicon(ROOT/'词条总表.tsv')
    def test_exact_hit_never_runs_difflib_loop(self):
        with patch('core.lookup.difflib.SequenceMatcher',side_effect=AssertionError('unnecessary fuzzy scan')):
            match=self.lex.best('伊莉娜')
        self.assertEqual(match.word,'伊莉娜');self.assertTrue(match.exact)
    def test_original_correction_priority_is_preserved(self):
        lex=Lexicon(ROOT/'词条总表.tsv');lex.corrections['伊莉娜']='可琳'
        self.assertEqual(lex.best('伊莉娜').word,'可琳')
    def test_fuzzy_matching_still_works(self):
        self.assertEqual(self.lex.best('伊莉那').word,'伊莉娜')
    def test_recent_unicode_read_is_not_repeated_just_for_log(self):
        cfg=copy.deepcopy(app.DEFAULT_CFG);cfg.update(verify=False,window_title='',input_region=[0,0,800,40],submit_styles=['unicode'])
        kb=Mock();lex=Mock();lex.candidates.return_value=[]
        eng=Engine(cfg,Mock(),lex,kb)
        eng.hwnd=lambda:10;eng.next_attempt=lambda text:('空中兵','kong zhong bing')
        eng.plan_for=lambda text:[('空中兵','kong zhong bing')]*4
        eng.clear_input=Mock();eng.read_input_retry=Mock(return_value='空中宾');eng.read_input=Mock(return_value='')
        events=[]
        kb.enter.side_effect=lambda:events.append('Enter-open')
        kb.type_unicode.side_effect=lambda word:events.append('Unicode:'+word)
        kb.enter_via.side_effect=lambda mode:events.append('Enter-send')
        with patch('core.engine.win.foreground',return_value=10),patch('core.engine.time.sleep'):
            eng.act(Target('空中兵',Match('空中兵','kong zhong bing',1,True),(10,10,80,20)),(0,0,800,600))
        self.assertEqual(events,['Enter-open','Unicode:空中兵','Enter-send'])
        self.assertEqual(eng.read_input_retry.call_count,1)
        self.assertFalse(eng.pause_flag)

if __name__=='__main__':unittest.main()
