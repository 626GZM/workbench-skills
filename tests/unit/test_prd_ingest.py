"""prd-doc-ingest：md 图片引用改写、html 降级、docx 图片、规则抽取、目录清单、残缺检查、缺外部命令降级。"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / 'skills' / 'prd-doc-ingest' / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))

import prd_ingest as pi  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32

MD = '''# 订单导出需求

## 目标

- 支持批量导出订单

## 接口约定

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /orders/export | 查询导出任务 |

## 验收条件

- 30 秒内生成导出文件
- 失败时给出错误码

## 待确认

- 单次导出上限多少条？

![内联](images/inline.png)

![上级](../shared/up.png)

![绝对]({absolute})
'''


HTML = '''<!doctype html>
<html><head><style>.x { color: red; }</style><script>var a = 1;</script></head>
<body>
<h1>订单导出需求</h1>
<p>导出订单为表格。</p>
<h2>验收条件</h2>
<ul><li>30 秒内生成</li><li>失败可重试</li></ul>
<table>
  <tr><th>方法</th><th>路径</th></tr>
  <tr><td>GET</td><td>/orders/export</td></tr>
</table>
<img src="pic.png" alt="流程图">
</body></html>
'''

DOCUMENT_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
            xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>订单导出需求</w:t></w:r></w:p>
    <w:p><w:r><w:t>导出订单为表格。</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>验收条件</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>30 秒内生成</w:t></w:r></w:p>
    <w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData>
      <a:blip r:embed="rId5"/>
    </a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
  </w:body>
</w:document>
'''

RELS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId5"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
    Target="media/image1.png"/>
</Relationships>
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


class IngestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src = self.root / 'src'
        self.dest = self.root / 'reqs' / 'REQ-101'

    def tearDown(self):
        self._tmp.cleanup()

    def make_md(self):
        (self.src / 'images').mkdir(parents=True, exist_ok=True)
        (self.src / 'images' / 'inline.png').write_bytes(PNG)
        (self.root / 'shared').mkdir(parents=True, exist_ok=True)
        (self.root / 'shared' / 'up.png').write_bytes(PNG)
        (self.root / 'shared' / 'abs.png').write_bytes(PNG)
        return write(self.src / 'PRD.md',
                     MD.format(absolute=(self.root / 'shared' / 'abs.png').as_posix()))

    def make_docx(self):
        path = self.src / 'PRD.docx'
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('word/document.xml', DOCUMENT_XML)
            z.writestr('word/_rels/document.xml.rels', RELS_XML)
            z.writestr('word/media/image1.png', PNG)
        return path


class IngestTest(IngestBase):
    def test_md落盘并改写跨目录图片引用(self):
        src = self.make_md()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pi.main(['ingest', '--src', str(src), '--dir', str(self.dest)])
        self.assertEqual(code, 0)

        result = json.loads(out.getvalue())
        self.assertEqual(result['title'], '订单导出需求')
        self.assertEqual(result['files'], ['PRD.md'])
        self.assertEqual(result['images'], 3)
        self.assertEqual(result['warnings'], [])

        text = (self.dest / 'PRD.md').read_text(encoding='utf-8')
        self.assertIn('images/inline.png', text)          # 同目录保持原路径
        self.assertIn('assets/up.png', text)
        self.assertIn('assets/abs.png', text)
        self.assertNotIn('../shared/up.png', text)
        self.assertTrue((self.dest / 'images' / 'inline.png').is_file())
        self.assertTrue((self.dest / 'assets' / 'up.png').is_file())
        self.assertTrue((self.dest / 'assets' / 'abs.png').is_file())

    def test_目标目录非空不加force报错(self):
        src = self.make_md()
        self.dest.mkdir(parents=True)
        (self.dest / 'keep.md').write_text('x', encoding='utf-8')
        with self.assertRaises(ValueError):
            pi.import_cmd(str(src), str(self.dest))

    def test_html转md保留层级列表表格和图片(self):
        src = write(self.src / 'PRD.html', HTML)
        (self.src / 'pic.png').write_bytes(PNG)

        result = pi.import_cmd(str(src), str(self.dest))
        self.assertEqual(result['images'], 1)
        self.assertEqual(result['files'], ['PRD.md'])

        text = (self.dest / 'PRD.md').read_text(encoding='utf-8')
        self.assertIn('# 订单导出需求', text)
        self.assertIn('## 验收条件', text)
        self.assertIn('- 30 秒内生成', text)
        self.assertIn('| 方法 | 路径 |', text)
        self.assertIn('| GET | /orders/export |', text)
        self.assertIn('![流程图](pic.png)', text)
        self.assertNotIn('color: red', text)
        self.assertNotIn('var a', text)
        self.assertTrue((self.dest / 'pic.png').is_file())

    def test_docx抽正文标题列表并从media取图(self):
        src = self.make_docx()
        result = pi.import_cmd(str(src), str(self.dest))
        self.assertEqual(result['images'], 1)
        self.assertEqual(result['warnings'], [])

        text = (self.dest / 'PRD.md').read_text(encoding='utf-8')
        self.assertIn('# 订单导出需求', text)
        self.assertIn('## 验收条件', text)
        self.assertIn('- 30 秒内生成', text)
        self.assertIn('![](assets/image1.png)', text)
        self.assertTrue((self.dest / 'assets' / 'image1.png').is_file())

    def test_pdf缺外部命令不崩只给warning(self):
        src = self.src / 'PRD.pdf'
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b'%PDF-1.4 not really')

        result = pi.import_cmd(str(src), str(self.dest),
                               pdf_cmd='definitely-not-a-real-pdf-cmd {src} {out}')
        self.assertEqual(result['files'], [])
        self.assertTrue(any('PDF 转换失败' in w for w in result['warnings']))
        self.assertFalse(list(self.dest.glob('*.md')))


class ExtractTest(IngestBase):
    def setUp(self):
        super().setUp()
        self.make_md()
        pi.import_cmd(str(self.src / 'PRD.md'), str(self.dest))

    def test_规则抽取目标验收接口和待确认(self):
        data = pi.extract_cmd(str(self.dest))
        self.assertEqual(data['files'], ['PRD.md'])

        goals = [g['text'] for g in data['goals']]
        self.assertIn('支持批量导出订单', goals)

        texts = [a['text'] for a in data['acceptance']]
        self.assertIn('30 秒内生成导出文件', texts)
        self.assertEqual(data['acceptance'][0]['id'], 'AC-01')

        apis = [(i['method'], i['path']) for i in data['interfaces']]
        self.assertIn(('GET', '/orders/export'), apis)

        questions = [q['text'] for q in data['questions']]
        self.assertTrue(any('单次导出上限' in q for q in questions))

    def test_extract写出到文件(self):
        out = self.dest / 'extract.json'
        data = pi.extract_cmd(str(self.dest), str(out))
        self.assertEqual(data['out'], str(out.resolve()))
        self.assertTrue(out.is_file())
        self.assertEqual(json.loads(out.read_text(encoding='utf-8'))['files'], ['PRD.md'])


class ListTest(IngestBase):
    def test_列出文件大小图片数和大纲(self):
        self.make_md()
        pi.import_cmd(str(self.src / 'PRD.md'), str(self.dest))

        data = pi.list_cmd(str(self.dest))
        self.assertEqual(len(data['files']), 1)
        item = data['files'][0]
        self.assertEqual(item['file'], 'PRD.md')
        self.assertGreater(item['size'], 0)
        self.assertEqual(item['images'], 3)
        self.assertEqual(item['title'], '订单导出需求')
        self.assertIn('验收条件', [s['title'] for s in item['sections']])
        self.assertGreater(data['total_size'], 0)
        self.assertEqual(data['total_images'], 3)
        self.assertEqual(data['assets'], 2)


class CheckTest(IngestBase):
    def test_找出失图和外链图片(self):
        self.dest.mkdir(parents=True)
        write(self.dest / 'PRD.md', '''# 订单导出需求

## 验收条件

- 能导出

![丢了](assets/none.png)

![外链](https://example.com/a.png)
''')
        data = pi.check_cmd(str(self.dest))
        self.assertFalse(data['ok'])
        types = [i['type'] for i in data['issues']]
        self.assertIn('missing_image', types)
        self.assertIn('remote_image', types)
        missing = [i for i in data['issues'] if i['type'] == 'missing_image'][0]
        self.assertEqual(missing['ref'], 'assets/none.png')
        self.assertIn('missing_image', data['summary']['by_type'])


class CliTest(unittest.TestCase):
    def test_help能跑(self):
        proc = subprocess.run([sys.executable, str(SCRIPT_DIR / 'prd_ingest.py'), '--help'],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        for cmd in ('ingest', 'list', 'extract', 'check'):
            self.assertIn(cmd, proc.stdout)


if __name__ == '__main__':
    unittest.main()
