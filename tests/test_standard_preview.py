"""标准库预览回归：图像与画布均为模拟对象，不创建窗口或发出输入。"""
import ast
import builtins
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


class FakePhoto:
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.samples = []

    def width(self):
        return self.w

    def height(self):
        return self.h

    def subsample(self, x, y):
        self.samples.append((x, y))
        return FakePhoto((self.w+x-1)//x, (self.h+y-1)//y)


class StandardPreviewTests(unittest.TestCase):
    def ui(self, width=400, height=250):
        ui = app.App.__new__(app.App)
        ui.root = Mock()
        ui.cv = Mock()
        ui.cv.winfo_width.return_value = width
        ui.cv.winfo_height.return_value = height
        ui.prev_var = Mock()
        ui.prev_var.get.return_value = True
        ui.prev_img = None
        ui._log = Mock()
        return ui

    def test_no_external_runtime_imports_and_can_import_without_site_packages(self):
        command = [sys.executable, '-S', '-c',
                   "import sys;sys.path.insert(0,sys.argv[1]);import app;"
                   "assert not any(n=='PIL' or n.startswith('PIL.') for n in sys.modules);"
                   "assert app.APP_NAME=='ff14 mokou ocr';print('STANDARD_LIBRARY_IMPORT_OK')", str(ROOT)]
        result = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn(b'STANDARD_LIBRARY_IMPORT_OK', result.stdout)

    def test_all_application_modules_have_only_standard_library_imports(self):
        for file in [ROOT/'app.py', *sorted((ROOT/'core').glob('*.py'))]:
            tree = ast.parse(file.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [name.name.split('.')[0] for name in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [(node.module or '').split('.')[0]]
                else:
                    continue
                for name in names:
                    self.assertTrue(name in sys.stdlib_module_names or name == 'core', (file.name, name))

    def test_small_image_keeps_original_photo_reference(self):
        ui = self.ui()
        photo = FakePhoto(200, 100)
        ui.prev_img = photo
        ui._show_img()
        self.assertIs(ui._tkimg, photo)
        self.assertEqual(ui._preview_factor, 1)
        self.assertEqual(photo.samples, [])
        self.assertIs(ui.cv.create_image.call_args.kwargs['image'], photo)

    def test_large_image_fits_canvas_using_native_subsample(self):
        ui = self.ui()
        photo = FakePhoto(933, 575)
        ui.prev_img = photo
        ui._show_img()
        self.assertEqual(photo.samples, [(3, 3)])
        self.assertLessEqual(ui._tkimg.width(), 400)
        self.assertLessEqual(ui._tkimg.height(), 250)
        ui.cv.create_text.assert_called_once()

    def test_boxes_follow_the_same_scale_as_native_image(self):
        ui = self.ui()
        ui.prev_img = FakePhoto(933, 575)
        target = SimpleNamespace(x=90, y=60, w=120, h=75)
        ui._draw_boxes({'targets': [target]})
        ui.cv.create_rectangle.assert_called_once_with(30., 20., 70., 45., outline='#00ff78', width=2)

    def test_disabled_preview_does_not_draw(self):
        ui = self.ui()
        ui.prev_img = FakePhoto(100, 100)
        ui.prev_var.get.return_value = False
        ui._draw_boxes({'targets': [SimpleNamespace(x=0, y=0, w=5, h=5)]})
        ui.cv.create_image.assert_not_called()
        ui.cv.create_rectangle.assert_not_called()

    def test_preview_loads_png_using_tk_photoimage(self):
        ui = self.ui()
        photo = FakePhoto(933, 575)
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp)/'.cache'/'preview.png'
            png.parent.mkdir()
            png.write_bytes(b'PNG-placeholder')
            with patch.object(app, 'BASE', temp), patch.object(app.tk, 'PhotoImage', return_value=photo) as loader:
                ui._load_preview()
            loader.assert_called_once_with(master=ui.root, file=str(png))
        self.assertIs(ui.prev_img, photo)

    def test_partial_png_write_keeps_last_good_preview(self):
        ui = self.ui()
        original = FakePhoto(80, 80)
        ui.prev_img = original
        with tempfile.TemporaryDirectory() as temp:
            png = Path(temp)/'.cache'/'preview.png'
            png.parent.mkdir()
            png.write_bytes(b'incomplete')
            with patch.object(app, 'BASE', temp), patch.object(app.tk, 'PhotoImage', side_effect=app.tk.TclError('incomplete PNG')):
                ui._load_preview()
        self.assertIs(ui.prev_img, original)
        ui.cv.create_image.assert_not_called()

    def test_input_crop_uses_same_standard_preview(self):
        ui = self.ui()
        ui.ocr = Mock()
        ui.ocr.save_last.return_value = {'ok': True}
        photo = FakePhoto(813, 46)
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'BASE', temp), patch.object(app.tk, 'PhotoImage', return_value=photo) as loader:
            ui._show_crop({'ok': True}, 'input')
            path = str(Path(temp)/'.cache'/'probe_input.png')
            ui.ocr.save_last.assert_called_once_with(path)
            loader.assert_called_once_with(master=ui.root, file=path)
        self.assertIs(ui.prev_img, photo)

    def test_crop_save_error_reports_without_loading_invalid_file(self):
        ui = self.ui()
        ui.ocr = Mock()
        ui.ocr.save_last.return_value = {'ok': False, 'err': 'no image'}
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'BASE', temp), patch.object(app.tk, 'PhotoImage') as loader:
            ui._show_crop({}, 'input')
        loader.assert_not_called()
        ui._log.assert_called_once()

    def test_missing_preview_does_not_create_image(self):
        ui = self.ui()
        with tempfile.TemporaryDirectory() as temp, patch.object(app, 'BASE', temp), patch.object(app.tk, 'PhotoImage') as loader:
            ui._load_preview()
        loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
