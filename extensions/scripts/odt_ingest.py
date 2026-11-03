"""Faithful, conservative ODT chapter ingest. No model or editorial processing."""
import argparse
import http.cookiejar
from html.parser import HTMLParser
import urllib.parse
import urllib.request
import urllib.error
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET


class IngestError(ValueError):
    pass


def command(program, *args, data=None):
    executable = shutil.which(str(program))
    if not executable:
        raise IngestError(f'Executable not found: {program}')
    result = subprocess.run([str(Path(executable).resolve()), *map(str, args)],
                            input=data, capture_output=True)
    if result.returncode or result.stderr.strip():
        raise IngestError(f'{program}: {result.stderr.decode(errors="replace")}')
    return result.stdout


def text(inlines):
    parts = []
    for node in inlines:
        tag = node['t']
        if tag == 'Str':
            parts.append(node['c'])
        elif tag in ('Space', 'SoftBreak', 'LineBreak'):
            parts.append(' ')
        elif tag in ('Emph', 'Strong', 'SmallCaps', 'Strikeout', 'Superscript', 'Subscript'):
            parts.append(text(node['c']))
        elif tag == 'Quoted':
            parts.append(text(node['c'][1]))
        else:
            raise IngestError(f'Unsupported heading inline: {tag}')
    return ''.join(parts)


ALLOWED = set('Para Plain Str Space SoftBreak LineBreak Emph Strong Quoted SingleQuote DoubleQuote '
              'SmallCaps Strikeout Superscript Subscript BlockQuote Note HorizontalRule Header'.split())


def unsupported(value):
    found = set()
    if isinstance(value, dict):
        if 't' in value and value['t'] not in ALLOWED:
            found.add(value['t'])
        for child in value.values():
            found.update(unsupported(child))
    elif isinstance(value, list):
        for child in value:
            found.update(unsupported(child))
    return found


def split_document(doc, source, level=1):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source):
        raise IngestError('Expected a stable Google Drive file ID')
    issues, chapters, outside = [], [], []
    current = None
    for index, block in enumerate(doc['blocks']):
        heading = block['t'] == 'Header' and block['c'][0] == level
        label = text(block['c'][2]) if heading else ''
        match = re.fullmatch(r'Chapter\s+([1-9][0-9]*)(?:\s*[:.\-–—]\s*.+)?', label, re.I) if heading else None
        if heading:
            current = None
            if match:
                number = int(match[1])
                if number != len(chapters) + 1:
                    issues.append(f'Block {index}: expected chapter {len(chapters)+1}, found {number}')
                current = {'number': number, 'title': label, 'blocks': []}
                chapters.append(current)
            else:
                issues.append(f'Block {index}: non-chapter heading {label!r}')
        elif block['t'] == 'Header':
            label = text(block['c'][2])
            if re.match(r'^Chapter\s+', label, re.I):
                issues.append(f'Block {index}: chapter heading at unexpected level {block["c"][0]}')
        elif block['t'] in ('Para', 'Plain'):
            try:
                label = text(block['c'])
            except IngestError:
                label = ''
            if re.match(r'^Chapter\s+(?:\d+|[IVXLCDM]+|one|two|three)\b', label, re.I):
                issues.append(f'Block {index}: possible unstyled chapter boundary {label!r}')
        bad = unsupported(block)
        if bad:
            issues.append(f'Block {index}: unsupported content {sorted(bad)}')
        if current is None:
            outside.append({'block': index, 'content': block})
        else:
            current['blocks'].append(block)
    if not chapters:
        issues.append('No explicit numbered chapter headings found')
    for chapter in chapters:
        if len(chapter['blocks']) < 2:
            issues.append(f'Chapter {chapter["number"]} has no body')
    if outside:
        issues.append(f'{len(outside)} non-chapter blocks retained in report')
    return chapters, outside, issues


def parse_odt(path, source, level=1, pandoc='pandoc'):
    # Some ODT constructs disappear before reaching Pandoc's AST. Refuse them
    # explicitly rather than presenting the conversion as complete.
    with zipfile.ZipFile(path) as archive:
        for member in ('content.xml', 'styles.xml'):
            tree = ET.fromstring(archive.read(member))
            for node in tree.iter():
                local = node.tag.rsplit('}', 1)[-1]
                if local in {'header', 'footer', 'header-left', 'footer-left'}:
                    clean = copy.deepcopy(node)
                    for parent in clean.iter():
                        for child in list(parent):
                            if child.tag.rsplit('}', 1)[-1] == 'page-number':
                                parent.remove(child)
                    if not ''.join(clean.itertext()).strip():
                        continue  # pagination alone is not manuscript content
                if local in {'annotation', 'annotation-end', 'tracked-changes', 'changed-region',
                             'table', 'frame', 'object', 'object-ole', 'image', 'text-box',
                             'header', 'footer', 'header-left', 'footer-left'}:
                    # Empty default headers and empty tracked-change containers are harmless.
                    if len(node) or (node.text or '').strip() or local in {'image','object','object-ole'}:
                        raise IngestError(f'Unsupported ODT construct in {member}: {local}')
                if local in {'p', 'h'} and not ''.join(node.itertext()).strip() and len(node) == 0:
                    raise IngestError('Empty ODT paragraph: verify whether this represents a scene break')
    doc = json.loads(command(pandoc, '--from=odt', '--to=json', path))
    chapters, outside, issues = split_document(doc, source, level)
    records = []
    for chapter in chapters:
        part = copy.deepcopy(doc)
        part['meta'] = {}
        part['blocks'] = chapter['blocks']
        body = command(pandoc, '--from=json', '--to=markdown+footnotes', '--wrap=none',
                       data=json.dumps(part).encode()).decode()
        records.append({'input_record': {'source': source, 'chapter': chapter['number']}, 'content': body})
    return {'records': records, 'titles': [c['title'] for c in chapters],
            'non_chapter_content': outside, 'document_metadata': doc.get('meta', {}),
            'issues': issues}


MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024


def public_reference(reference):
    url = urllib.parse.urlsplit(reference)
    if url.scheme != 'https' or url.hostname not in {'drive.google.com', 'drive.usercontent.google.com'} or url.username or url.password or url.port:
        raise IngestError('Expected an HTTPS Google Drive file URL')
    match = re.fullmatch(r'/file/d/([A-Za-z0-9_-]+)(?:/[^/]*)?/?', url.path)
    query = urllib.parse.parse_qs(url.query)
    ids = [match[1]] if match else query.get('id', []) if url.path in {'/open', '/uc', '/download'} else []
    if len(ids) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]+', ids[0]):
        raise IngestError('Expected one Google Drive file ID, not a folder or native Google Doc')
    resourcekeys = query.get('resourcekey', [])
    if len(resourcekeys) > 1 or any(not re.fullmatch(r'[A-Za-z0-9_-]+', key) for key in resourcekeys):
        raise IngestError('Invalid Drive resource key')
    params = {'export': 'download', 'id': ids[0]}
    if resourcekeys:
        params['resourcekey'] = resourcekeys[0]
    return ids[0], 'https://drive.google.com/uc?' + urllib.parse.urlencode(params)


def trusted_download_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and not parsed.port and (host in {'drive.google.com', 'drive.usercontent.google.com'}
                                     or host.endswith('.googleusercontent.com')))


class DriveRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not trusted_download_url(newurl):
            raise IngestError('Drive redirected outside public download hosts; check anyone-with-link access')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class DownloadForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action, self.fields, self.active = None, {}, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form' and attrs.get('id') == 'download-form':
            if attrs.get('method', 'get').lower() != 'get':
                return
            self.action, self.active = attrs.get('action'), True
        elif tag == 'input' and self.active and attrs.get('type') == 'hidden' and attrs.get('name'):
            self.fields[attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.active = False


def download_public(reference, destination, opener=None):
    source, url = public_reference(reference)
    opener = opener or urllib.request.build_opener(DriveRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    for attempt in range(2):
        with opener.open(url, timeout=60) as response:
            if not trusted_download_url(response.geturl()):
                raise IngestError('Unexpected public download destination')
            prefix = response.read(4096)
            if prefix.startswith(b'PK\x03\x04'):
                total = len(prefix)
                with destination.open('xb') as output:
                    output.write(prefix)
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise IngestError('Drive download exceeds 128 MiB limit')
                        output.write(chunk)
                with zipfile.ZipFile(destination) as archive:
                    if archive.read('mimetype') != b'application/vnd.oasis.opendocument.text':
                        raise IngestError('Drive file is not an original ODT manuscript')
                return source
            page = prefix + response.read(1024 * 1024)
            form = DownloadForm()
            form.feed(page.decode('utf-8', errors='replace'))
            if attempt or not form.action or form.fields.get('id') != source or not trusted_download_url(form.action):
                raise IngestError('Drive returned a sharing, login, quota, or unsupported download page; verify anyone-with-link access to the original ODT')
            url = form.action + ('&' if '?' in form.action else '?') + urllib.parse.urlencode(form.fields)
    raise IngestError('Public Drive download did not complete')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', help='Public Google Drive file URL or authenticated gdrive: ODT path')
    parser.add_argument('--book-title', required=True)
    parser.add_argument('--target-dir', type=Path, required=True, help='Existing empty destination directory')
    parser.add_argument('--heading-level', type=int, choices=range(1, 7), default=1)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report', type=Path, required=True, help='New JSON report file; never overwritten')
    parser.add_argument('--writer', default='writenew')
    args = parser.parse_args()
    public = not args.reference.startswith('gdrive:')
    if not public and not args.reference.lower().endswith('.odt'):
        parser.error('gdrive: reference must name an ODT file')
    # rclone notices are not discarded; suppress routine log output, retain errors.
    flags = ['--log-level=ERROR', '--retries=1', '--low-level-retries=1', '--timeout=30s']
    try:
        def stat():
            return json.loads(command('rclone', 'lsjson', args.reference, '--stat', *flags))
        with tempfile.TemporaryDirectory(prefix='odt-ingest-') as directory:
            local = Path(directory) / 'source.odt'
            if public:
                source = download_public(args.reference, local)
            else:
                before = stat()
                source = before.get('ID', '')
                if before.get('IsDir') or not source:
                    raise IngestError('Drive did not return an original file and stable ID')
                command('rclone', 'copyto', args.reference, local, *flags)
                after = stat()
                if any(before.get(k) != after.get(k) for k in ('ID', 'Size', 'ModTime')):
                    raise IngestError('Drive source changed during retrieval')
            report = parse_odt(local, source, args.heading_level)
        report.update(source=source, reference=args.reference, book_title=args.book_title,
                      intended_files=[f'Chapter {i:04}.md' for i in range(1, len(report['records'])+1)] + ['Book.md'])
        with args.report.open('x', encoding='utf-8') as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write('\n')
        if report['issues']:
            raise IngestError('Review required: ' + '; '.join(report['issues']))
        stream = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in report['records']).encode()
        options = ['--book-title', args.book_title, '--target-dir', args.target_dir.resolve()]
        if args.dry_run:
            options.append('--dry-run')
        result = command(args.writer, *options, data=stream)
        print(result.decode(), end='')
    except (IngestError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError, KeyError, urllib.error.URLError) as error:
        parser.exit(1, f'odt-ingest: {error}\n')


if __name__ == '__main__':
    main()
