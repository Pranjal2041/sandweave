#!/usr/bin/env python3
"""Compare public research requests directly and through user-supplied proxies.

Credentials are read from a private JSON file, never copied into the report.
Response bodies are local evidence, not intended for redistribution.
Requires requests. Sampling separately requires pyarrow.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import random
import re
import time
from urllib.parse import quote, unquote, urlsplit

import requests


def load_proxies(path):
    routes = []
    for index, value in enumerate(json.loads(path.read_text())):
        if isinstance(value, str):
            parsed = urlsplit(value)
            if parsed.scheme != 'http' or not parsed.hostname:
                raise ValueError('this comparison harness expects HTTP proxy URLs')
            port = parsed.port if parsed.port is not None else 80
            if not 1 <= port <= 65535:
                raise ValueError('proxy port must be between 1 and 65535')
            value = {'id': f'proxy-{index + 1}', 'host': parsed.hostname, 'port': port,
                     'username': unquote(parsed.username or ''), 'password': unquote(parsed.password or '')}
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', value['id']):
            raise ValueError('proxy IDs must be safe file-name components')
        routes.append(value)
    return routes


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.text = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1
        if tag == 'a':
            self.links.extend(value for key, value in attrs if key == 'href' and value)

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def classify(status, url, body, keywords=()):
    if body.startswith('%PDF-'):
        return {'signals': [], 'format': 'pdf', 'text_characters': None, 'keyword_hits': [], 'links': None}
    parser = VisibleText()
    parser.feed(body)
    text = re.sub(r'\s+', ' ', ' '.join(parser.text)).strip()
    patterns = {
        'challenge': ('our systems have detected unusual traffic', 'verify you are human',
                      'verify that you are human', 'enable javascript and cookies to continue',
                      'anomaly.js', 'anomaly-modal', 'cf-chl-', 'px-captcha',
                      'please complete the security check'),
        'consent': ('consent.google.com', 'before you continue to google'),
        'proxy_auth': ('proxy authentication required',),
        'access_denied': ('access denied', 'request blocked', 'the request could not be satisfied'),
    }
    haystack = (url + '\n' + body[:300000]).lower()
    signals = [name for name, fragments in patterns.items() if any(x in haystack for x in fragments)]
    if status == 407 and 'proxy_auth' not in signals:
        signals.append('proxy_auth')
    search_results = None
    host = urlsplit(url).hostname or ''
    if host.endswith('bing.com'):
        search_results = len(re.findall(r'class=[\"\'][^\"\']*\bb_algo\b', body))
    elif host.endswith('duckduckgo.com'):
        search_results = len(re.findall(r'class=[\"\'][^\"\']*\bresult__a\b', body))
    elif host in ('google.com', 'www.google.com') and '/search' in url:
        search_results = len(re.findall(r'<h3\b', body))
    if status == 200 and search_results == 0:
        signals.append('no_search_results_in_html')
    return {'signals': signals, 'text_characters': len(text), 'search_results': search_results,
            'keyword_hits': [word for word in keywords if word.casefold() in text.casefold()],
            'links': len(parser.links), 'text_preview': text[:650]}


def probe(route, target, output, repeat):
    result = {'route': route['id'], 'request': target['id'], 'kind': target['kind'],
              'task_row': target.get('task_row'), 'url': target['url'], 'repeat': repeat}
    session = requests.Session()
    session.trust_env = False
    if route.get('host'):
        authority = quote(route['username'], safe='') + ':' + quote(route['password'], safe='')
        proxy = f"http://{authority}@{route['host']}:{route['port']}"
        session.proxies = {'http': proxy, 'https': proxy}
    started = time.monotonic()
    try:
        with session.get(target['url'], timeout=(8, 20), stream=True, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }) as response:
            chunks, size = [], 0
            for chunk in response.iter_content(16384):
                chunks.append(chunk)
                size += len(chunk)
                if size >= 2 * 1024**2 or time.monotonic() - started > 35:
                    break
            body = b''.join(chunks)
            result.update(status=response.status_code, final_url=response.url, bytes=len(body),
                          truncated=size >= 2 * 1024**2,
                          content_type=response.headers.get('Content-Type', ''),
                          redirects=[r.status_code for r in response.history],
                          sha256=hashlib.sha256(body).hexdigest())
            result.update(classify(response.status_code, response.url,
                                   body.decode(response.encoding or 'utf-8', errors='replace'),
                                   target.get('keywords', [])))
            name = f"{route['id']}-{target['id']}-{repeat}.body"
            (output / 'bodies' / name).write_bytes(body)
            result['body'] = 'bodies/' + name
    except requests.RequestException as error:
        # Exception messages may contain authenticated proxy URLs.
        result.update(error=type(error).__name__)
    finally:
        session.close()
    result['seconds'] = round(time.monotonic() - started, 3)
    return result


def sample(args):
    import pyarrow.parquet as pq
    session = requests.Session()
    session.trust_env = False
    revision = args.revision
    if not revision:
        metadata = session.get('https://huggingface.co/api/datasets/osunlp/QUEST-RL-Data', timeout=30)
        metadata.raise_for_status()
        revision = metadata.json()['sha']
    args.output.mkdir(parents=True, exist_ok=False)
    path = args.output / 'train.parquet'
    url = f'https://huggingface.co/datasets/osunlp/QUEST-RL-Data/resolve/{revision}/data/train.parquet'
    with session.get(url, timeout=60, stream=True) as response:
        response.raise_for_status()
        with path.open('wb') as stream:
            for chunk in response.iter_content(1024**2):
                stream.write(chunk)
    rows = pq.read_table(path, columns=['prompt', 'extra_info', 'rl_task_category'], use_threads=False).to_pylist()
    chosen = []
    for index in random.Random(args.seed).sample(range(len(rows)), args.count):
        row = rows[index]
        extra = ast.literal_eval(row['extra_info']) if isinstance(row['extra_info'], str) else row['extra_info']
        chosen.append({'row': index, 'task_id': extra.get('original_task_id'),
                       'category': row['rl_task_category'], 'question': row['prompt'][0]['content']})
    result = {'dataset': 'osunlp/QUEST-RL-Data', 'revision': revision, 'rows': len(rows),
              'seed': args.seed, 'parquet_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'tasks': chosen}
    (args.output / 'sample.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


def run(args):
    routes = [{'id': 'direct'}, *load_proxies(args.proxies)]
    if args.routes:
        routes = [r for r in routes if r['id'] in args.routes.split(',')]
    targets = json.loads(args.requests.read_text())['requests']
    for item in [*routes, *targets]:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', item['id']):
            raise ValueError('IDs must be safe file-name components')
    if args.output.exists():
        raise ValueError('Use a new output directory to preserve previous observations')
    (args.output / 'bodies').mkdir(parents=True, mode=0o700)
    results = []
    jobs = [(r, t, n) for n in range(args.repeats) for t in targets for r in routes]
    random.Random(42).shuffle(jobs)
    with (args.output / 'results.jsonl').open('w') as log, ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(probe, r, t, args.output, n) for r, t, n in jobs]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            log.write(json.dumps(result) + '\n'); log.flush()
            print(result['route'], result['request'], result.get('status', result.get('error')),
                  result.get('signals', []), result['seconds'], flush=True)
    report = {'finished_at': datetime.now(timezone.utc).isoformat(),
              'routes': [r['id'] for r in routes], 'requests': targets, 'results': results}
    (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


def browser(args):
    from playwright.sync_api import sync_playwright
    routes = [{'id': 'direct'}, *load_proxies(args.proxies)]
    routes = [r for r in routes if not args.routes or r['id'] in args.routes.split(',')]
    targets = json.loads(args.requests.read_text())['requests']
    if any(not re.fullmatch(r'[a-zA-Z0-9_-]+', target['id']) for target in targets):
        raise ValueError('request IDs must be safe file-name components')
    if args.output.exists():
        raise ValueError('Use a new output directory')
    args.output.mkdir(parents=True, mode=0o700)
    results = []
    with sync_playwright() as p:
        chromium = p.chromium.launch(headless=True)
        try:
            for route in routes:
                settings = {'viewport': {'width': 1280, 'height': 900}, 'locale': 'en-US'}
                if route.get('host'):
                    settings['proxy'] = {'server': f"http://{route['host']}:{route['port']}",
                                         'username': route['username'], 'password': route['password']}
                context = chromium.new_context(**settings)
                context.route('**/*', lambda route: route.abort() if route.request.resource_type in (
                    'image', 'media', 'font') else route.continue_())
                try:
                    for target in targets:
                        page = context.new_page()
                        result = {'route': route['id'], 'request': target['id'], 'url': target['url']}
                        started = time.monotonic()
                        try:
                            response = page.goto(target['url'], wait_until='domcontentloaded', timeout=30000)
                            page.wait_for_timeout(5000)
                            html = page.content()
                            result.update(status=response.status if response else None, final_url=page.url)
                            result.update(classify(result['status'],page.url,html,target.get('keywords', [])))
                            result['visible_text'] = page.locator('body').inner_text(timeout=5000)[:1000]
                            name = route['id'] + '-' + target['id']
                            (args.output / (name + '.html')).write_text(html)
                            page.screenshot(path=str(args.output / (name + '.png')))
                        except Exception as error:
                            result['error'] = type(error).__name__
                        finally:
                            page.close()
                        result['seconds'] = round(time.monotonic()-started,3)
                        results.append(result)
                        (args.output / 'report.json').write_text(json.dumps(results,indent=2)+'\n')
                        print(result['route'],result['request'],result.get('status',result.get('error')),
                              result.get('signals'),result.get('search_results'),flush=True)
                finally:
                    context.close()
        finally:
            chromium.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    sampler = commands.add_parser('sample')
    sampler.add_argument('--seed', type=int, default=20260911)
    sampler.add_argument('--revision', help='dataset commit; defaults to its current revision')
    sampler.add_argument('--count', type=int, default=6)
    sampler.add_argument('--output', type=Path, required=True)
    runner = commands.add_parser('run')
    runner.add_argument('--proxies', type=Path, required=True)
    runner.add_argument('--requests', type=Path, required=True)
    runner.add_argument('--output', type=Path, required=True)
    runner.add_argument('--routes')
    runner.add_argument('--parallel', type=int, default=4)
    runner.add_argument('--repeats', type=int, default=1)
    rendered = commands.add_parser('browser')
    rendered.add_argument('--proxies', type=Path, required=True)
    rendered.add_argument('--requests', type=Path, required=True)
    rendered.add_argument('--output', type=Path, required=True)
    rendered.add_argument('--routes')
    args = parser.parse_args()
    {'sample': sample, 'run': run, 'browser': browser}[args.command](args)
