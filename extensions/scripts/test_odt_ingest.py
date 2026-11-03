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


if __name__=='__main__':
    unittest.main()
