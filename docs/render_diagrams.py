"""Render docs/diagrams/*.mmd to SVG and PNG with pinned Mermaid and Chromium.

Python standard library only. No access to application data or live services.
"""
import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote
from urllib.request import urlopen
import xml.etree.ElementTree as ET

VERSION = '11.12.0'
SHA256 = '07e37dfa97b337ccc85365d57eddf99b9706f09db3b59b260d0333b23b343c4b'
URL = f'https://cdn.jsdelivr.net/npm/mermaid@{VERSION}/dist/mermaid.min.js'
DOCS = Path(__file__).resolve().parent


def browser_path(explicit):
    candidates = [explicit, shutil.which('chromium'), shutil.which('google-chrome'),
                  r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                  r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe']
    return next((Path(p) for p in candidates if p and Path(p).is_file()), None)


def chrome(browser, profile, url, *args):
    command = [str(browser), '--headless=new', '--disable-gpu', '--no-first-run',
               '--run-all-compositor-stages-before-draw',
               '--no-default-browser-check', '--disable-background-networking',
               '--disable-extensions', '--hide-scrollbars', '--force-device-scale-factor=1',
               '--allow-file-access-from-files', f'--user-data-dir={profile}',
               '--virtual-time-budget=20000', *args, url]
    result = subprocess.run(command, capture_output=True, timeout=60,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(f'Chromium exited {result.returncode}: {result.stderr[-2000:]!r}')
    return result.stdout.decode('utf-8', errors='replace')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser')
    parser.add_argument('--mermaid-js', type=Path)
    args = parser.parse_args()
    browser = browser_path(args.browser)
    if not browser:
        raise SystemExit('Chrome/Edge/Chromium not found; specify --browser')
    sources = sorted((DOCS / 'diagrams').glob('*.mmd'))
    if not sources:
        raise SystemExit('No Mermaid sources found')
    images = DOCS / 'images'
    images.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='atropos-uml-') as temporary:
        work = Path(temporary)
        library = args.mermaid_js.read_bytes() if args.mermaid_js else urlopen(URL, timeout=30).read()
        if hashlib.sha256(library).hexdigest() != SHA256:
            raise SystemExit('Mermaid SHA-256 mismatch')
        (work / 'mermaid.min.js').write_bytes(library)
        payload = [{'name': p.stem, 'code': p.read_text(encoding='utf-8')} for p in sources]
        page = '''<!doctype html><meta charset="utf-8"><body><pre id="render-output">PENDING</pre>
<script src="mermaid.min.js"></script><script>
mermaid.initialize({startOnLoad:false,theme:'base',securityLevel:'strict',
  fontFamily:'Segoe UI, Arial, sans-serif',fontSize:18,
  themeVariables:{primaryColor:'#edf4ff',primaryTextColor:'#15243b',primaryBorderColor:'#4b72a4',
    lineColor:'#54708f',secondaryColor:'#eef8f4',tertiaryColor:'#fff7e8',
    actorBkg:'#edf4ff',actorTextColor:'#15243b',noteBkgColor:'#fff7e8',noteTextColor:'#26364d',
    signalTextColor:'#15243b',fontSize:'18px'},
  sequence:{useMaxWidth:false,wrap:true,diagramMarginX:32,diagramMarginY:24,
    actorMargin:60,messageMargin:36,noteMargin:16,width:190},
  class:{useMaxWidth:false,padding:16,nodeSpacing:45,rankSpacing:70},
  flowchart:{htmlLabels:false,useMaxWidth:false}});
(async()=>{try {const output=[]; let n=0;
for(const item of PAYLOAD) {const rendered=await mermaid.render('uml'+n++,item.code);
output.push({name:item.name,svg:encodeURIComponent(rendered.svg)});}
document.getElementById('render-output').textContent=JSON.stringify({ok:true,output});
}catch(error){document.getElementById('render-output').textContent=JSON.stringify({ok:false,error:String(error)});}})();
</script></body>'''.replace('PAYLOAD', json.dumps(payload, ensure_ascii=True))
        source = work / 'render.html'
        source.write_text(page, encoding='utf-8')
        dom = chrome(browser, work / 'profile', source.as_uri(), '--dump-dom')
        found = re.search(r'<pre id="render-output">(.*?)</pre>', dom, re.S)
        if not found:
            raise RuntimeError('Missing Mermaid output')
        try:
            result = json.loads(html.unescape(found.group(1)))
        except json.JSONDecodeError:
            raise RuntimeError('Mermaid did not finish: ' + found.group(1)[:500]) from None
        if not result['ok']:
            raise RuntimeError(result['error'])
        manifest = {'mermaid_version': VERSION, 'mermaid_sha256': SHA256,
                    'browser': browser.name, 'images': []}
        for item in result['output']:
            # Mermaid's HTML labels serialize void <br> tags; normalize for SVG XML readers.
            svg = unquote(item['svg']).replace('<br>', '<br/>')
            try:
                element = ET.fromstring(svg)
            except ET.ParseError as error:
                column = error.position[1]
                raise RuntimeError(f'{item["name"]}: invalid SVG near {svg[max(0, column-100):column+100]}') from error
            viewbox = list(map(float, element.attrib['viewBox'].split()))
            width, height = math.ceil(viewbox[2]), math.ceil(viewbox[3])
            # Explicit dimensions ensure the browser exports the complete viewBox.
            root_tag = re.search(r'<svg\b[^>]*>', svg).group(0)
            resized = re.sub(r'\s(?:width|height)="[^"]*"', '', root_tag)
            resized = resized[:-1] + f' width="{width}" height="{height}">'
            svg = svg.replace(root_tag, resized, 1)
            target = images / (item['name'] + '.svg')
            target.write_text(svg, encoding='utf-8')
            screenshot = images / (item['name'] + '.png')
            wrapper = work / 'screenshot.html'
            wrapper.write_text('<!doctype html><meta charset="utf-8"><style>'
                               'html,body{margin:0;background:white;}body{padding:16px;}'
                               '</style>' + svg, encoding='utf-8')
            chrome(browser, work / 'profile', wrapper.as_uri(),
                   f'--window-size={width + 32},{height + 32}', f'--screenshot={screenshot}')
            if not screenshot.is_file() or screenshot.stat().st_size < 1000:
                raise RuntimeError(f'PNG export failed: {screenshot}')
            manifest['images'].append({'name': item['name'], 'svg_width': width, 'svg_height': height,
                'png_width': width + 32, 'png_height': height + 32,
                'source_sha256': hashlib.sha256((DOCS / 'diagrams' / (item['name'] + '.mmd')).read_bytes()).hexdigest(),
                'svg_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                'png_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest()})
            print(f'{item["name"]}: {width} x {height}', flush=True)
        (images / 'render-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
