import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import io
import zipfile

spec = importlib.util.spec_from_file_location('ingest', Path(__file__).with_name('odt_ingest.py'))
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)


def para(value):
    return {'t':'Para','c':[{'t':'Str','c':value}]}


def header(number):
    return {'t':'Header','c':[1,['',[],[]],[{'t':'Str','c':f'Chapter {number}'}]]}


class IngestTests(unittest.TestCase):
    def document(self):
        return {'pandoc-api-version':[1,23,1], 'meta':{}, 'blocks':[
            header(1), para('“Original dialogue,” she said.'),
            {'t':'Para','c':[{'t':'Emph','c':[{'t':'Str','c':'Italic words'}]}]},
            {'t':'BlockQuote','c':[para('A quotation.')]},
            para('* * *'),
            {'t':'Para','c':[{'t':'Str','c':'Text'},{'t':'Note','c':[para('Footnote text.')]}]},
            header(2), para('Second chapter.') ]}

    def test_odt_fixture_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'representative.odt'
            fixture.write_bytes(ingest.command('pandoc','-f','json','-t','odt',data=json.dumps(self.document()).encode()))
            result=ingest.parse_odt(fixture,'stable_Drive_ID')
        self.assertEqual(result['issues'],[])
        self.assertEqual([r['input_record'] for r in result['records']],
                         [{'source':'stable_Drive_ID','chapter':1},{'source':'stable_Drive_ID','chapter':2}])
        first=result['records'][0]['content']
        for expected in ['Original dialogue','*Italic words*','> A quotation.','Footnote text.',r'\* \* \*']:
            self.assertIn(expected,first)
        self.assertIn('Second chapter.',result['records'][1]['content'])
        self.assertNotIn('Second chapter.',first)

    def test_missing_ambiguous_and_non_chapter_content(self):
        for blocks in [[para('No headings')], [header(2),para('Body')],
                       [header(1),para('Chapter 2'),para('Body')]]:
            self.assertTrue(ingest.split_document({'blocks':blocks},'ID')[2])
        doc=self.document()
        doc['blocks'].insert(0,para('Front matter'))
        doc['blocks'] += [{'t':'Header','c':[1,['',[],[]],[{'t':'Str','c':'Afterword'}]]},para('End matter')]
        chapters,outside,issues=ingest.split_document(doc,'ID')
        self.assertEqual(len(chapters),2)
        self.assertEqual([b['block'] for b in outside],[0,9,10])
        self.assertTrue(issues)

    def test_odt_annotations_are_not_silently_lost(self):
        original=ingest.command('pandoc','-f','json','-t','odt',data=json.dumps(self.document()).encode())
        with tempfile.TemporaryDirectory() as directory:
            fixture=Path(directory)/'annotated.odt'
            with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(fixture,'w') as dest:
                for name in source.namelist():
                    content=source.read(name)
                    if name=='content.xml':
                        content=content.replace(b'</office:text>',b'<office:annotation><text:p>Review comment</text:p></office:annotation></office:text>')
                    dest.writestr(name,content)
            with self.assertRaisesRegex(ingest.IngestError,'annotation'):
                ingest.parse_odt(fixture,'ID')

    def test_unsupported_material_is_reported(self):
        doc=self.document()
        doc['blocks'].append({'t':'RawBlock','c':['html','<unconverted>']})
        self.assertIn('unsupported content','; '.join(ingest.split_document(doc,'ID')[2]))


class PublicDriveTests(unittest.TestCase):
    def odt_bytes(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        return output.getvalue()

    def opener(self, responses):
        class Response(io.BytesIO):
            def geturl(self):
                return 'https://drive.usercontent.google.com/download?id=original_ID'
        class Opener:
            urls = []
            def open(self, url, timeout):
                self.urls.append(url)
                return Response(responses.pop(0))
        return Opener()

    def test_public_urls_and_resource_key(self):
        for url in ['https://drive.google.com/file/d/original_ID/view?usp=sharing',
                    'https://drive.google.com/open?id=original_ID']:
            self.assertEqual(ingest.public_reference(url)[0], 'original_ID')
        self.assertIn('resourcekey=0-key', ingest.public_reference('https://drive.google.com/file/d/original_ID/view?resourcekey=0-key')[1])
        for url in ['http://drive.google.com/file/d/ID/view', 'https://example.com/file/d/ID/view',
                    'https://drive.google.com/drive/folders/ID', 'https://docs.google.com/document/d/ID/edit',
                    'https://drive.google.com/open?id=a&id=b']:
            with self.assertRaises(ingest.IngestError):
                ingest.public_reference(url)

    def test_direct_public_download_retains_source_id(self):
        data = self.odt_bytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'original.odt'
            source = ingest.download_public('https://drive.google.com/file/d/original_ID/view', path, self.opener([data]))
            self.assertEqual(source, 'original_ID')
            self.assertEqual(path.read_bytes(), data)

    def test_confirmation_download(self):
        form = b'<form id="download-form" action="https://drive.usercontent.google.com/download" method="get"><input type="hidden" name="id" value="original_ID"><input type="hidden" name="confirm" value="t"></form>'
        opener = self.opener([form, self.odt_bytes()])
        with tempfile.TemporaryDirectory() as directory:
            ingest.download_public('https://drive.google.com/file/d/original_ID/view', Path(directory)/'source.odt', opener)
        self.assertIn('confirm=t', opener.urls[-1])

    def test_login_and_foreign_confirmation_are_rejected(self):
        pages = [b'<html>Sign in</html>', b'<form id="download-form" action="https://example.com/download"><input type="hidden" name="id" value="original_ID"></form>']
        with tempfile.TemporaryDirectory() as directory:
            for page in pages:
                with self.assertRaises(ingest.IngestError):
                    ingest.download_public('https://drive.google.com/file/d/original_ID/view', Path(directory)/'source.odt', self.opener([page]))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_non_odt_zip_is_rejected(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('mimetype','application/zip')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ingest.IngestError, 'not an original ODT'):
                ingest.download_public('https://drive.google.com/file/d/original_ID/view',Path(directory)/'source.odt',self.opener([output.getvalue()]))


if __name__=='__main__':
    unittest.main()
