#!/usr/bin/env python3
"""Check a built documentation site and exercise its browser interactions.

Run after `uvx --with-requirements docs/requirements.txt mkdocs build --strict`.
Uses an ephemeral local HTTP listener or --url; never starts a sandbox or cluster.
"""
import argparse
import ast
from functools import partial
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import textwrap
import threading
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PREFIX = '/sandweave/'


class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.ids, self.links = set(), []
        self.feed(path.read_text())

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'id' in values:
            self.ids.add(values['id'])
        if tag == 'a' and values.get('href'):
            self.links.append(values['href'])


def check_content(site):
    examples = 0
    for path in sorted((ROOT / 'docs').glob('*.md')):
        for match in re.finditer(r'^([ ]*)```python[^\n]*\n(.*?)^\1```', path.read_text(), re.M | re.S):
            ast.parse(textwrap.dedent(match[2]), filename=str(path))
            examples += 1
    pages = {path: Page(path) for path in site.rglob('*.html')}
    links = 0
    for path, page in pages.items():
        for link in page.links:
            parsed = urlsplit(link)
            if parsed.netloc or parsed.scheme:
                continue
            if parsed.path.startswith('/'):
                assert parsed.path.startswith(PREFIX), (path, link)
                target = site / unquote(parsed.path.removeprefix(PREFIX))
            else:
                target = path.parent / unquote(parsed.path) if parsed.path else path
            target = target.resolve()
            assert target.is_relative_to(site), (path, link)
            if target.is_dir():
                target /= 'index.html'
            assert target.exists(), (path, link)
            if parsed.fragment and target in pages:
                assert unquote(parsed.fragment) in pages[target].ids, (path, link)
            links += 1
    assert (site / 'llms.txt').is_file()
    return dict(pages=len(pages), python_examples=examples, internal_links=links)


def check_browser(site, output, url=None):
    from playwright.sync_api import sync_playwright, expect

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if not self.path.startswith(PREFIX):
                self.send_error(404)
                return
            self.path = '/' + self.path.removeprefix(PREFIX)
            super().do_GET()

    server = None
    if url is None:
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(site)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port) + PREFIX
    url = url.rstrip('/') + '/'
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1100}, color_scheme='light',
                                              permissions=['clipboard-read', 'clipboard-write'])
                page = context.new_page()
                errors, failed = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('response', lambda response: failed.append(response.url) if response.status >= 400 else None)
                page.goto(url)
                expect(page.locator('h1')).to_have_text(re.compile(r'^Sandboxes for your agents¶?$'))
                page.get_by_role('button', name='Copy to clipboard').first.click()
                assert page.evaluate('navigator.clipboard.readText()') == 'uv pip install sandweave'
                page.screenshot(path=str(output / 'home-desktop.png'), full_page=True, animations='disabled')
                page.locator('label[title="Switch to dark mode"]').click()
                expect(page.locator('body')).to_have_attribute('data-md-color-scheme', 'slate')
                page.screenshot(path=str(output / 'home-dark.png'), full_page=True, animations='disabled')
                page.locator('label[title="Switch to light mode"]').click()
                search = page.get_by_role('textbox', name='Search')
                search.fill('offline')
                expect(page.locator('.md-search-result__list')).to_contain_text('Networking')
                page.screenshot(path=str(output / 'search.png'), full_page=True, animations='disabled')
                page.keyboard.press('Escape')
                page.locator('.md-sidebar--primary').get_by_role('link', name='Networking', exact=True).click()
                expect(page.locator('h1')).to_have_text(re.compile(r'^Networking¶?$'))
                assert page.url.endswith('/networking/')
                page.locator('.md-sidebar--primary').get_by_role('link', name='Docker images', exact=True).click()
                expect(page.locator('h1')).to_have_text(re.compile(r'^Docker images¶?$'))
                page.get_by_role('button', name='Copy to clipboard').first.click()
                assert 'Sandbox(image="docker://python:3.12-slim")' in page.evaluate('navigator.clipboard.readText()')
                page.screenshot(path=str(output / 'images-desktop.png'), full_page=True, animations='disabled')
                page.locator('.md-sidebar--primary').get_by_role('link', name='Connect a cluster', exact=True).click()
                expect(page.locator('h1')).to_have_text(re.compile(r'^Connect a cluster¶?$'))
                page.screenshot(path=str(output / 'clusters-desktop.png'), full_page=True, animations='disabled')
                page.goto(url + 'installation/')
                page.get_by_text('pip', exact=True).click()
                expect(page.locator('.tabbed-block:visible')).to_contain_text('python -m pip install sandweave')
                page.set_viewport_size({'width': 390, 'height': 844})
                page.goto(url)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'home-mobile.png'), full_page=True, animations='disabled')
                page.locator('.md-header label[for="__drawer"]').click()
                expect(page.locator('#__drawer')).to_be_checked()
                page.screenshot(path=str(output / 'mobile-menu.png'), animations='disabled')
                page.locator('.md-sidebar--primary .md-nav__title').filter(has_text='Get started').click()
                page.locator('.md-nav--primary > .md-nav__list > .md-nav__item--nested > label').filter(has_text='Sandboxes').click()
                page.locator('.md-sidebar--primary').get_by_role('link', name='Lifetime and cleanup', exact=True).click()
                expect(page.locator('h1')).to_have_text(re.compile(r'^Lifetime and cleanup¶?$'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'lifecycle-mobile.png'), full_page=True, animations='disabled')
                page.goto(url + 'images/')
                expect(page.locator('h1')).to_have_text(re.compile(r'^Docker images¶?$'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'images-mobile.png'), full_page=True, animations='disabled')
                assert not errors, errors
                assert not failed, failed
                context.close()
            finally:
                browser.close()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--url', help='Check a published site instead of serving the local build (requires --browser)')
    parser.add_argument('--output', type=Path, default=ROOT / 'runs/docs-acceptance')
    args = parser.parse_args()
    if args.url and not args.browser:
        parser.error('--url requires --browser')
    args.output.mkdir(parents=True, exist_ok=True)
    result = check_content((ROOT / 'site').resolve())
    if args.browser:
        check_browser((ROOT / 'site').resolve(), args.output, args.url)
        result['browser'] = 'passed'
    (args.output / 'checks.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
