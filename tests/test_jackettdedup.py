# Offline tests for jackettdedup.py. A local HTTP server plays Jackett, so the plugin's
# real HTTP code runs. From the repo root:  python tests/test_jackettdedup.py
#
# In order: stand-ins for qBittorrent's modules, a torrent fixture, the fake Jackett, a
# base class that runs one search, the tests.

import base64
import contextlib
import hashlib
import http.server
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
from xml.sax.saxutils import escape

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TORZNAB_NS = 'xmlns:torznab="http://torznab.com/schemas/2015/feed"'
GiB = 1024 ** 3


# ------------------------------------------------------- qBittorrent stand-ins
# Two modules that only exist inside qBittorrent. prettyPrinter prints one '|'-joined
# line per result the way qBittorrent 5.1's novaprinter 1.52 does: a '|' in the name
# becomes a space and the size is converted to bytes. It rejects a '|' inside any other
# field, and a line break anywhere, which the real one would print as two rows. Every
# row printed is also kept in PRINTED as (seconds since PRINTED_FROM, the row as printed),
# which is what the search tests read.

PRINTED = []
PRINTED_FROM = time.monotonic()


def _pretty_printer(result):
    for key, value in result.items():
        if '\n' in str(value) or '\r' in str(value):
            raise ValueError(f"line break in {key}")
    for key in ('link', 'size', 'seeds', 'leech', 'engine_url', 'desc_link', 'pub_date'):
        if '|' in str(result.get(key, '')):
            raise ValueError(f"unexpected delimiter in {key}")
    size = int(result['size'].split(' ')[0]) if isinstance(result['size'], str) else result['size']
    printed = dict(result, name=result['name'].replace('|', ' '), size=size)
    PRINTED.append((time.monotonic() - PRINTED_FROM, printed))
    print('|'.join([printed['link'], printed['name'], str(size), str(printed['seeds']),
                    str(printed['leech']), printed['engine_url'], printed.get('desc_link', ''), str(printed.get('pub_date', -1))]))


novaprinter = types.ModuleType('novaprinter')
novaprinter.prettyPrinter = _pretty_printer
helpers = types.ModuleType('helpers')
helpers.enable_socks_proxy = lambda enabled: None
helpers.download_file = lambda url: url + ' ' + url
sys.modules['novaprinter'] = novaprinter
sys.modules['helpers'] = helpers


# -------------------------------------------------------------- torrent fixture

# Encodes ints, bytes, lists and dicts (sorted keys, as the spec requires) as bencode.
def bencode(value):
    if isinstance(value, int):
        return b'i%de' % value
    if isinstance(value, bytes):
        return b'%d:%s' % (len(value), value)
    if isinstance(value, list):
        return b'l' + b''.join(bencode(item) for item in value) + b'e'
    return b'd' + b''.join(bencode(key) + bencode(item) for key, item in sorted(value.items())) + b'e'


# A small single-file .torrent with every bencode form nested inside `info`. TORRENT_HASH
# is what the plugin must compute from TORRENT_FILE; TORRENT_SIZE is its file size.
TORRENT_INFO = {b'length': 123456, b'name': b'Some.File.mkv', b'piece length': 262144, b'pieces': b'\x00' * 40,
                b'nested': {b'a': [1, 2, b'x'], b'b': b'd:e'}}
TORRENT_FILE = bencode({b'announce': b'udp://tr.example:6969', b'announce-list': [[b'udp://tr.example:6969'], [b'udp://tier2.example:1']],
                        b'creation date': 1700000000, b'info': TORRENT_INFO, b'url-list': [b'http://seed/']})
TORRENT_HASH = hashlib.sha1(bencode(TORRENT_INFO)).hexdigest()
TORRENT_SIZE = 123456

# A multi-file .torrent: the size is the sum of the files' lengths.
MULTI_FILE_TORRENT = bencode({b'announce': b'udp://tr.example:6969',
                              b'info': {b'name': b'Pack', b'piece length': 262144, b'pieces': b'\x00' * 20,
                                        b'files': [{b'length': 10, b'path': [b'a.mkv']}, {b'length': 5, b'path': [b'b.srt']}]}})

# A v2-only .torrent (BitTorrent v2): `meta version` 2, a `file tree`, no v1 `pieces`. Its
# key is "v2:" + the SHA-256 of `info`. A hybrid carries both layouts and keeps the v1 key.
V2_INFO = {b'meta version': 2, b'name': b'v2.only.mkv', b'piece length': 65536,
           b'file tree': {b'v2.only.mkv': {b'': {b'length': 4321, b'pieces root': b'\x11' * 32}}}}
V2_FILE = bencode({b'announce': b'udp://v2.example:1', b'info': V2_INFO, b'piece layers': {}})
V2_KEY = 'v2:' + hashlib.sha256(bencode(V2_INFO)).hexdigest()
HYBRID_INFO = {**TORRENT_INFO, b'meta version': 2,
               b'file tree': {b'Some.File.mkv': {b'': {b'length': 123456, b'pieces root': b'\x22' * 32}}}}
HYBRID_FILE = bencode({b'announce': b'udp://tr.example:6969', b'info': HYBRID_INFO})
HYBRID_HASH = hashlib.sha1(bencode(HYBRID_INFO)).hexdigest()


# Makes a distinct, repeatable infohash from a short name, so fixtures can refer to hashes by name.
def fake_hash(name):
    return hashlib.sha1(name.encode()).hexdigest()


# ----------------------------------------------------------------- fake Jackett

# Jackett's two endpoints on a random local port: the indexer list / search API, and the
# download links (/dl/<indexer>/?path=<key>) that hash lookups fetch. Results are added
# with add_result; a download link answers per download_replies[key]:
#   ('magnet', hash)          302 to a magnet with that hash and one tracker
#   ('torrent', bytes)        the bytes, as a .torrent file
#   ('html',)                 500 with an HTML body, as Jackett does when a site fails
#   ('page',)                 200 with an HTML body, a site's error page passed through
#   ('sleep', seconds, hash)  wait, then the magnet reply
# A site's search can be made to fail the way a real Jackett reports a broken site: HTTP
# 400 (or 429 for a rate-limited site) with an <error code="900"> body.
# download_requests records every download request as (seconds since start, key).
class FakeJackett:
    def __init__(self):
        # id -> {'type': public|private|semi-private, 'items': [item xml]}
        self.indexers = {}
        self.download_replies = {}
        # indexer id -> seconds before its search answers
        self.search_delays = {}
        # indexer id -> how many searches fail before one succeeds; -1 = every one
        self.search_failures = {}
        # indexers whose failures are rate limits (HTTP 429) instead of errors (HTTP 400)
        self.rate_limited = set()
        # indexer id -> searches received
        self.search_attempts = {}
        # seconds before the indexer list answers
        self.discovery_delay = 0
        # replaces the indexer list reply when set (bytes), with this status code
        self.discovery_body = None
        self.discovery_status = 200
        self.download_requests = []
        self.started = time.monotonic()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                if url.path.startswith('/api/v2.0/indexers/'):
                    indexer_id = url.path.split('/')[4]
                    if query.get('t') == ['indexers']:
                        time.sleep(fake.discovery_delay)
                        if fake.discovery_body is not None:
                            self.send_response(fake.discovery_status)
                            self.end_headers()
                            self.wfile.write(fake.discovery_body)
                            return
                        body = '<?xml version="1.0" encoding="UTF-8"?><indexers>' + ''.join(
                            f'<indexer id="{id_}"><title>{id_}</title><type>{indexer["type"]}</type></indexer>'
                            for id_, indexer in fake.indexers.items()) + '</indexers>'
                    else:
                        fake.search_attempts[indexer_id] = fake.search_attempts.get(indexer_id, 0) + 1
                        failures_left = fake.search_failures.get(indexer_id, 0)
                        if failures_left != 0:
                            if failures_left > 0:
                                fake.search_failures[indexer_id] = failures_left - 1
                            self.send_response(429 if indexer_id in fake.rate_limited else 400)
                            self.send_header('Content-Type', 'application/xml')
                            self.end_headers()
                            # the description is a .NET stack trace; Jackett writes its line breaks as &#xA;
                            self.wfile.write(b'<?xml version="1.0" encoding="UTF-8"?><error code="900" description="Jackett.Common.IndexerException: '
                                             b'Exception (' + indexer_id.encode() + b'): Cloudflare challenge failed&#xA;   at Jackett.Common.Indexers.BaseWebIndexer" />')
                            return
                        time.sleep(fake.search_delays.get(indexer_id, 0))
                        if indexer_id == 'all':
                            items = [item for indexer in fake.indexers.values() for item in indexer['items']]
                        else:
                            items = fake.indexers[indexer_id]['items']
                        body = f'<?xml version="1.0" encoding="UTF-8"?><rss {TORZNAB_NS}><channel>' + ''.join(items) + '</channel></rss>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/xml')
                    self.end_headers()
                    self.wfile.write(body.encode())
                    return
                if url.path.startswith('/dl/'):
                    key = query.get('path', [''])[0]
                    reply = fake.download_replies.get(key, ('html',))
                    fake.download_requests.append((time.monotonic() - fake.started, key))
                    if reply[0] == 'sleep':
                        time.sleep(reply[1])
                        reply = ('magnet', reply[2])
                    if reply[0] == 'magnet':
                        self.send_response(302)
                        self.send_header('Location', f'magnet:?xt=urn:btih:{reply[1]}&dn=resolved&tr=udp://r.example:1/announce')
                        self.end_headers()
                    elif reply[0] == 'torrent':
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/x-bittorrent')
                        self.end_headers()
                        self.wfile.write(reply[1])
                    elif reply[0] == 'page':
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.end_headers()
                        self.wfile.write(b'<html>The site answered with its error page</html>')
                    else:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(b'<html>BencodeParser failed</html>')
                    return
                self.send_response(404)
                self.end_headers()

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # Adds one search result to an indexer (created on first use).
    #   size: None = not reported
    #   infohash, magnet: omit both for a row Jackett knows no hash for
    #   download_key: key into download_replies; the row links to /dl/<indexer>/?path=<key>
    #   details_url: the row's <comments>, the plugin's lookup key
    def add_result(self, indexer_id, indexer_type, title, seeds, size, infohash=None, magnet=None, download_key=None, details_url=None):
        self.indexers.setdefault(indexer_id, {'type': indexer_type, 'items': []})
        link = f'{self.url}/dl/{indexer_id}/?jackett_apikey=K&path={download_key or "none"}'
        attrs = f'<torznab:attr name="seeders" value="{seeds}"/><torznab:attr name="peers" value="{seeds + 5}"/>'
        if infohash:
            attrs += f'<torznab:attr name="infohash" value="{infohash}"/>'
        if magnet:
            attrs += f'<torznab:attr name="magneturl" value="{escape(magnet, {chr(34): "&quot;"})}"/>'
        size_xml = f'<size>{size}</size>' if size is not None else ''
        details_xml = f'<comments>{escape(details_url)}</comments>' if details_url else ''
        items = self.indexers[indexer_id]['items']
        items.append(f"<item><title>{escape(title)}</title><jackettindexer id='{indexer_id}'>{indexer_id}</jackettindexer>"
                     f"<link>{escape(link)}</link><guid>http://x/{indexer_id}/{len(items)}</guid>{details_xml}"
                     f"<pubDate>Tue, 21 Oct 2026 10:00:00 +0000</pubDate>{size_xml}{attrs}</item>")

    # Adds an indexer with no results, for one that is only there to fail.
    def add_site(self, indexer_id, indexer_type='public'):
        self.indexers.setdefault(indexer_id, {'type': indexer_type, 'items': []})

    # The download keys fetched so far, in order: one per hash lookup or download.
    def requested_keys(self):
        return [key for _, key in self.download_requests]

    # Stops the server; called by each test's tearDown.
    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------- harness

# Imports the plugin fresh from a temp folder holding the given settings (it reads them at import).
def load_plugin(folder, settings):
    shutil.copy(os.path.join(REPO, 'jackettdedup.py'), folder)
    with open(os.path.join(folder, 'jackettdedup.json'), 'w') as settings_file:
        json.dump(settings, settings_file)
    sys.path.insert(0, folder)
    sys.modules.pop('jackettdedup', None)
    try:
        return importlib.import_module('jackettdedup')
    finally:
        sys.path.remove(folder)


# Base for tests that run a search against a FakeJackett.
class SearchTest(unittest.TestCase):
    def setUp(self):
        self.jackett = FakeJackett()
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        self.jackett.close()
        sys.modules.pop('jackettdedup', None)
        shutil.rmtree(self.folder, ignore_errors=True)

    # Runs one search with short deadlines: first results at 2 s, stop at 8 s.
    #   settings: overrides for jackettdedup.json
    #   returns: (rows as the stand-in printer printed them, each with 'at' = seconds after
    #             the start; seconds the search took)
    def run_search(self, query='name 2018', **settings):
        global PRINTED_FROM
        base = {'api_key': 'K', 'url': self.jackett.url, 'concurrent_searches': 8, 'show_results_after': 2, 'max_search_time': 8}
        base.update(settings)
        plugin = load_plugin(self.folder, base)
        PRINTED.clear()
        PRINTED_FROM = time.monotonic()
        # the stand-in printer validates and records each row; its output is just not shown
        with contextlib.redirect_stdout(io.StringIO()):
            plugin.jackettdedup().search(urllib.parse.quote(query), 'all')
        rows = [dict(result, at=at) for at, result in PRINTED]
        return rows, time.monotonic() - PRINTED_FROM

    # The tracker URLs in a shown row's magnet link.
    @staticmethod
    def trackers(row):
        return set(urllib.parse.parse_qs(row['link'].split('?', 1)[1]).get('tr', []))

    # The debug log the search wrote (needs debug_log=True in run_search).
    def log_text(self):
        with open(os.path.join(self.folder, 'jackettdedup.log')) as log_file:
            return log_file.read()


# ------------------------------------------------------------------------ tests

class HashHelpers(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.plugin = load_plugin(self.folder, {'api_key': 'K'})

    def tearDown(self):
        sys.modules.pop('jackettdedup', None)
        shutil.rmtree(self.folder, ignore_errors=True)

    # Anything that is not a torrent, or is cut short, gives None, not an exception. A hybrid
    # torrent keeps its v1 hash; a v2-only one gets its v2 key.
    def test_hash_from_torrent(self):
        self.assertEqual(self.plugin._hash_from_torrent(TORRENT_FILE), TORRENT_HASH)
        self.assertIsNone(self.plugin._hash_from_torrent(b'<html>nope</html>'))
        self.assertIsNone(self.plugin._hash_from_torrent(b'd4:infod1:ai1e'))
        self.assertIsNone(self.plugin._hash_from_torrent(b'd4:infod4:name5:ab'))
        self.assertIsNone(self.plugin._hash_from_torrent(bencode({b'zzz': 1})))
        self.assertEqual(self.plugin._hash_from_torrent(HYBRID_FILE), HYBRID_HASH)
        self.assertEqual(self.plugin._hash_from_torrent(V2_FILE), V2_KEY)

    # Hex (any case) and base32 magnets give the same lowercase hex hash. A v2-only magnet
    # gives the v2 key; a hybrid magnet, with both parts, the v1 hash.
    def test_hash_from_magnet(self):
        hex_hash = 'ab' * 20
        base32_hash = base64.b32encode(bytes.fromhex(hex_hash)).decode()
        self.assertEqual(self.plugin._hash_from_magnet(f'magnet:?xt=urn:btih:{hex_hash.upper()}'), hex_hash)
        self.assertEqual(self.plugin._hash_from_magnet(f'magnet:?xt=urn:btih:{base32_hash}&dn=x'), hex_hash)
        self.assertIsNone(self.plugin._hash_from_magnet('http://not.a.magnet'))
        self.assertEqual(self.plugin._hash_from_magnet(f'magnet:?xt=urn:btmh:1220{V2_KEY[3:].upper()}&dn=x'), V2_KEY)
        self.assertEqual(self.plugin._hash_from_magnet(f'magnet:?xt=urn:btih:{hex_hash}&xt=urn:btmh:1220{V2_KEY[3:]}'), hex_hash)

    # Site tags, brackets and punctuation vanish; words survive in lowercase, in order, in
    # any script.
    def test_title_words(self):
        strip_patterns = self.plugin.jackettdedup()._strip_patterns
        self.assertEqual(self.plugin._title_words('Name (2018) 720p [Bengali-Dub] x264 - 1XBET[TGx]', strip_patterns),
                         ('name', '2018', '720p', 'bengali', 'dub', 'x264', '1xbet'))
        self.assertEqual(self.plugin._title_words('www.Site.com - Name 2018 [YTS.MX]', strip_patterns), ('name', '2018'))
        self.assertEqual(self.plugin._title_words('Name [2018, США, комедия] Dub', strip_patterns), ('name', '2018', 'сша', 'комедия', 'dub'))

    # Trackers come from `announce` and every tier of `announce-list`, deduped, in order;
    # the size from info.length, or the sum of info.files; a non-torrent decodes to None.
    def test_trackers_and_size_from_torrent(self):
        torrent = self.plugin._torrent_dict(TORRENT_FILE)
        self.assertEqual(self.plugin._trackers_from_torrent(torrent), ['udp://tr.example:6969', 'udp://tier2.example:1'])
        self.assertEqual(self.plugin._size_from_torrent(torrent), TORRENT_SIZE)
        self.assertEqual(self.plugin._size_from_torrent(self.plugin._torrent_dict(MULTI_FILE_TORRENT)), 15)
        self.assertIsNone(self.plugin._torrent_dict(b'<html>nope</html>'))
        self.assertIsNone(self.plugin._torrent_dict(bencode([1, 2])))


class Settings(SearchTest):
    # With no jackettdedup.json, the connection settings come from the official plugin's
    # jackett.json and every other key is written with its default.
    def test_seeded_from_official_config(self):
        with open(os.path.join(self.folder, 'jackett.json'), 'w') as official_file:
            json.dump({'api_key': 'SEEDED', 'url': self.jackett.url + '/', 'thread_count': 3, 'tracker_first': True}, official_file)
        shutil.copy(os.path.join(REPO, 'jackettdedup.py'), self.folder)
        sys.path.insert(0, self.folder)
        sys.modules.pop('jackettdedup', None)
        try:
            plugin = importlib.import_module('jackettdedup')
        finally:
            sys.path.remove(self.folder)
        with open(os.path.join(self.folder, 'jackettdedup.json')) as settings_file:
            settings = json.load(settings_file)
        self.assertEqual((settings['api_key'], settings['concurrent_searches'], settings['indexers_before_title'], settings['show_results_after']),
                         ('SEEDED', 3, True, 20))
        # requests go to the address without a trailing slash; the plugin identifies itself
        # to qBittorrent with one, so its rows never match the official plugin's address
        self.assertEqual(plugin.jackettdedup.api_url, self.jackett.url)
        self.assertEqual(plugin.jackettdedup.url, self.jackett.url + '/')

    # A settings file from an older version: settings that no longer exist are dropped and
    # new ones added with their default; a quoted number still works as a number.
    def test_file_from_older_version_is_updated(self):
        plugin = load_plugin(self.folder, {'api_key': 'K', 'hold_for_lookups': 20, 'lookup_size_tolerance': 0.07, 'show_results_after': '15'})
        with open(os.path.join(self.folder, 'jackettdedup.json')) as settings_file:
            settings = json.load(settings_file)
        self.assertNotIn('hold_for_lookups', settings)
        self.assertNotIn('lookup_size_tolerance', settings)
        self.assertEqual(settings['size_tolerance'], 0.05)
        self.assertEqual(plugin.jackettdedup().cfg.show_results_after, 15)

    # An unset API key gives one error row, like the official plugin.
    def test_missing_api_key_shows_error_row(self):
        rows, _ = self.run_search(api_key='YOUR_API_KEY_HERE')
        self.assertEqual(len(rows), 1)
        self.assertIn('api key error', rows[0]['name'])

    # A non-XML indexer list (a proxy login page), a Jackett <error> (wrong key, as HTTP
    # 400 the way Jackett sends it, and as 200), and an empty list each give one error row
    # instead of an exception or silence.
    def test_bad_indexer_list_shows_error_row(self):
        # a well-formed page and a broken one
        for body in (b'<html><body>Please log in</body></html>', b'<html><body>Please log in'):
            self.jackett.discovery_body = body
            rows, _ = self.run_search()
            self.assertEqual(len(rows), 1)
            self.assertIn('not an indexer list', rows[0]['name'])
        self.jackett.discovery_body = b'<?xml version="1.0" encoding="UTF-8"?><error code="100" description="Invalid API Key" />'
        for status in (400, 200):
            self.jackett.discovery_status = status
            rows, _ = self.run_search()
            self.assertEqual(len(rows), 1)
            self.assertIn('Invalid API Key', rows[0]['name'])
        self.jackett.discovery_body = None
        rows, _ = self.run_search()
        self.assertEqual(len(rows), 1)
        self.assertIn('no indexers', rows[0]['name'])

    # max_search_time counts from the start, slow indexer list included.
    def test_max_search_time_includes_discovery(self):
        self.jackett.discovery_delay = 1.0
        self.jackett.add_result('tpb', 'public', 'Something', 1, 1000, infohash='a' * 40, magnet=f'magnet:?xt=urn:btih:{"a" * 40}')
        self.jackett.search_delays['tpb'] = 30
        _, seconds = self.run_search(show_results_after=1, max_search_time=2)
        self.assertLess(seconds, 2.6)

    # Jackett not running: one row saying so, at once.
    def test_jackett_unreachable_shows_error_row(self):
        rows, seconds = self.run_search(url='http://127.0.0.1:9')
        self.assertEqual(len(rows), 1)
        self.assertIn('could not get the indexer list from Jackett (connection error)', rows[0]['name'])
        self.assertLess(seconds, 5.0)


class SiteFailures(SearchTest):
    # A site whose search fails once is searched again and its rows merge as usual; a site
    # that fails every time gives one error row carrying Jackett's message; a rate-limited
    # site (HTTP 429) is not retried.
    def test_failed_site_is_retried(self):
        infohash = 'a' * 40
        self.jackett.add_result('tpb', 'public', 'Ubuntu 26.04 Desktop', 300, 6 * GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.add_result('flaky', 'public', 'Ubuntu 26.04 Desktop', 100, 6 * GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.add_site('dead')
        self.jackett.add_site('limited')
        self.jackett.search_failures = {'flaky': 1, 'dead': -1, 'limited': -1}
        self.jackett.rate_limited = {'limited'}
        # the retry waits 2 s, so the first showing must come after it for the copy to merge
        rows, _ = self.run_search(debug_log=True, show_results_after=4, search_retries=1)
        self.assertEqual(self.jackett.search_attempts, {'tpb': 1, 'flaky': 2, 'dead': 2, 'limited': 1})
        merged = [row for row in rows if infohash in row['link']]
        self.assertEqual(len(merged), 1)
        self.assertIn('[tpb, flaky]', merged[0]['name'])
        errors = [row['name'] for row in rows if row['name'].startswith('Jackett:')]
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("'dead' failed after 2 attempts: HTTP 400: Exception (dead): Cloudflare challenge failed!" in name for name in errors), errors)
        self.assertTrue(any("'limited' failed after 1 attempt: HTTP 429" in name for name in errors), errors)
        self.assertEqual(len(rows), 3)
        # flaky and dead needed a second attempt; dead and limited failed for good
        self.assertIn('sites_retried=2 sites_failed=2', self.log_text())
        self.assertIn('! dead', self.log_text())

    # search_retries 0 reports the first failure at once.
    def test_no_retry_when_off(self):
        self.jackett.add_result('flaky', 'public', 'Something', 1, 1000, infohash='b' * 40, magnet=f'magnet:?xt=urn:btih:{"b" * 40}')
        self.jackett.search_failures = {'flaky': 1}
        rows, _ = self.run_search(search_retries=0)
        self.assertEqual(self.jackett.search_attempts, {'flaky': 1})
        self.assertEqual(len(rows), 1)
        self.assertIn("'flaky' failed after 1 attempt:", rows[0]['name'])


class Downloads(SearchTest):
    # One fetch per download: a magnet redirect prints "<magnet> <link>", a .torrent is
    # saved to a temp file and printed as "<path> <link>", and an error page from Jackett
    # raises instead of saving the page as a torrent.
    def test_download_fetches_once(self):
        self.jackett.download_replies = {'as-magnet': ('magnet', 'a' * 40), 'as-torrent': ('torrent', TORRENT_FILE), 'as-html': ('html',)}
        plugin = load_plugin(self.folder, {'api_key': 'K', 'url': self.jackett.url})
        engine = plugin.jackettdedup()
        magnet_link = f'{self.jackett.url}/dl/x/?path=as-magnet'
        torrent_link = f'{self.jackett.url}/dl/x/?path=as-torrent'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            engine.download_torrent(magnet_link)
            engine.download_torrent(torrent_link)
            # the fake answers 500 with the page, as Jackett does
            with self.assertRaises(urllib.error.HTTPError):
                engine.download_torrent(f'{self.jackett.url}/dl/x/?path=as-html')
        magnet_line, torrent_line = output.getvalue().splitlines()
        self.assertTrue(magnet_line.startswith(f'magnet:?xt=urn:btih:{"a" * 40}') and magnet_line.endswith(' ' + magnet_link), magnet_line)
        saved_path = torrent_line[:-len(' ' + torrent_link)]
        with open(saved_path, 'rb') as saved:
            self.assertEqual(saved.read(), TORRENT_FILE)
        os.remove(saved_path)
        self.assertEqual(self.jackett.requested_keys(), ['as-magnet', 'as-torrent', 'as-html'])

    # Jackett answering a download link with a web page and status 200 (a site's own error
    # page passed through) raises instead of saving the page as a .torrent.
    def test_download_of_error_page_raises(self):
        self.jackett.download_replies = {'as-page': ('page',)}
        plugin = load_plugin(self.folder, {'api_key': 'K', 'url': self.jackett.url})
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                plugin.jackettdedup().download_torrent(f'{self.jackett.url}/dl/x/?path=as-page')

    # A download of a Jackett link goes direct even when qBittorrent passed an HTTP proxy
    # in: the link is on the Jackett host, which a proxy elsewhere cannot reach. The proxy
    # here is a dead port, so going through it would fail.
    def test_download_ignores_proxy_for_jackett_links(self):
        self.jackett.download_replies = {'as-magnet': ('magnet', 'a' * 40)}
        os.environ['http_proxy'] = os.environ['https_proxy'] = 'http://127.0.0.1:9'
        try:
            plugin = load_plugin(self.folder, {'api_key': 'K', 'url': self.jackett.url})
            with contextlib.redirect_stdout(io.StringIO()) as output:
                plugin.jackettdedup().download_torrent(f'{self.jackett.url}/dl/x/?path=as-magnet')
        finally:
            os.environ.pop('http_proxy', None)
            os.environ.pop('https_proxy', None)
        self.assertTrue(output.getvalue().startswith(f'magnet:?xt=urn:btih:{"a" * 40}'), output.getvalue())


class HashMerging(SearchTest):
    # One torrent on three indexers (uppercase hash, lowercase hash, base32 magnet with no
    # hash attribute) becomes one row: top seed count, every tracker, the web seed, all three
    # names. A '|' in a title is left to qBittorrent's printer, which shows it as a space
    # (escaping it here too would show %7C); a line break and doubled spaces in a title are
    # collapsed, so the row prints as one line. Every row names this plugin's identity
    # address, the Jackett url with a trailing slash.
    def test_same_hash_becomes_one_row(self):
        infohash = 'a' * 40
        base32_hash = base64.b32encode(bytes.fromhex(infohash)).decode()
        self.jackett.add_result('1337x', 'public', 'Ubuntu 26.04 Desktop', 120, 6 * GiB, infohash=infohash.upper(),
                                magnet=f'magnet:?xt=urn:btih:{infohash}&dn=Ubuntu&tr=udp://a.example:1/announce&ws=http://seed.example/file')
        self.jackett.add_result('tpb', 'public', 'Ubuntu 26.04 Desktop [TPB]', 300, 6 * GiB, infohash=infohash,
                                magnet=f'magnet:?xt=urn:btih:{infohash}&dn=Ubuntu&tr=udp://b.example:1/announce')
        self.jackett.add_result('knaben', 'public', 'Ubuntu 26.04 Desktop', 110, 6 * GiB,
                                magnet=f'magnet:?xt=urn:btih:{base32_hash}&tr=udp://c.example:1/announce')
        self.jackett.add_result('tpb', 'public', 'Pipe|in|title', 5, 1500, infohash='c' * 40, magnet=f'magnet:?xt=urn:btih:{"c" * 40}')
        self.jackett.add_result('tpb', 'public', 'Line one\n  Line   two', 4, 1600, infohash='d' * 40, magnet=f'magnet:?xt=urn:btih:{"d" * 40}')
        rows, _ = self.run_search()
        self.assertEqual(len(rows), 3)
        merged = next(row for row in rows if infohash in row['link'])
        self.assertEqual(merged['seeds'], 300)
        self.assertTrue(merged['name'].startswith('Ubuntu 26.04 Desktop [TPB] [tpb, 1337x +1]'), merged['name'])
        self.assertEqual(self.trackers(merged), {'udp://a.example:1/announce', 'udp://b.example:1/announce', 'udp://c.example:1/announce'})
        self.assertIn('ws=', merged['link'])
        self.assertTrue(any(row['name'].startswith('Pipe in title') for row in rows))
        self.assertTrue(any(row['name'].startswith('Line one Line two [tpb]') for row in rows))
        self.assertTrue(all(row['engine_url'] == self.jackett.url + '/' for row in rows))

    # When the best-seeded copy is on a semi-private tracker, the row links to its .torrent
    # (passkey) instead of a magnet.
    def test_private_best_source_keeps_torrent_link(self):
        infohash = 'b' * 40
        self.jackett.add_result('rutracker', 'semi-private', 'Russian Dub 1080p', 900, 4 * GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.add_result('tpb', 'public', 'Russian Dub 1080p', 10, 4 * GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        rows, _ = self.run_search()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['link'].startswith(self.jackett.url + '/dl/rutracker/'), rows[0]['link'])

    # A private copy merged under a better-seeded public copy keeps the merged row private:
    # it is never folded behind a lookalike and never hides one.
    def test_private_copy_keeps_merged_row_unfolded(self):
        infohash = 'd' * 40
        other_hash = 'e' * 40
        self.jackett.add_result('tpb', 'public', 'Name 2018 1080p WEB-DL', 900, 2 * GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.add_result('rutracker', 'semi-private', 'Name 2018 1080p WEB-DL', 10, 2 * GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.add_result('knaben', 'public', 'Name 2018 1080p WEB-DL', 500, 2 * GiB, infohash=other_hash, magnet=f'magnet:?xt=urn:btih:{other_hash}')
        rows, _ = self.run_search()
        self.assertEqual(len(rows), 2)
        self.assertFalse(any('+1 alt' in row['name'] for row in rows))

    # A v2-only torrent (no v1 hash) on two indexers, one as a urn:btmh magnet and one as a
    # .torrent reached by a lookup, becomes one row with a urn:btmh magnet carrying both
    # sites' trackers. A hybrid .torrent reached by a lookup merges by its v1 hash as usual.
    def test_v2_only_copies_merge(self):
        v2_hex = V2_KEY[3:]
        self.jackett.add_result('tpb', 'public', 'V2 Only Release 2026', 40, 4321, magnet=f'magnet:?xt=urn:btmh:1220{v2_hex}&tr=udp://a.example:1/announce')
        self.jackett.download_replies = {'lime-v2': ('torrent', V2_FILE), 'lime-hybrid': ('torrent', HYBRID_FILE)}
        self.jackett.add_result('lime', 'public', 'V2 Only Release 2026', 20, 4321, download_key='lime-v2', details_url='https://lime/v2')
        self.jackett.add_result('tpb', 'public', 'Some File mkv', 50, TORRENT_SIZE, infohash=HYBRID_HASH, magnet=f'magnet:?xt=urn:btih:{HYBRID_HASH}')
        self.jackett.add_result('lime', 'public', 'Some File mkv', 10, TORRENT_SIZE, download_key='lime-hybrid', details_url='https://lime/hybrid')
        rows, _ = self.run_search(fold_lookalikes=False)
        self.assertEqual(len(rows), 2)
        v2_row = next(row for row in rows if 'btmh' in row['link'])
        self.assertTrue(v2_row['link'].startswith(f'magnet:?xt=urn:btmh:1220{v2_hex}'), v2_row['link'])
        self.assertIn('[tpb, lime]', v2_row['name'])
        self.assertEqual(self.trackers(v2_row), {'udp://a.example:1/announce', 'udp://v2.example:1'})
        hybrid_row = next(row for row in rows if HYBRID_HASH in row['link'])
        self.assertIn('[tpb, lime]', hybrid_row['name'])


class HashLookups(SearchTest):
    # With the second round off, only likely duplicates from public indexers are looked up.
    # A magnet redirect and a .torrent both resolve and merge; a failed lookup leaves the
    # row as-is; an unrelated row and a private-tracker row cost no request.
    def test_lookups_only_likely_duplicates(self):
        infohash = 'a' * 40
        self.jackett.add_result('tpb', 'public', 'Ubuntu 26.04 Desktop amd64', 300, 6 * GiB, infohash=infohash,
                                magnet=f'magnet:?xt=urn:btih:{infohash}&tr=udp://tpb.example:1/announce')
        self.jackett.add_result('tpb', 'public', 'Some File mkv', 50, TORRENT_SIZE, infohash=TORRENT_HASH, magnet=f'magnet:?xt=urn:btih:{TORRENT_HASH}')
        self.jackett.download_replies = {'lime-ubuntu': ('magnet', infohash), 'x-fail': ('html',), 'tdl-some': ('torrent', TORRENT_FILE)}
        # 3% smaller from display rounding: still a candidate
        self.jackett.add_result('lime', 'public', 'Ubuntu 26 04 Desktop amd64', 90, int(5.83 * GiB), download_key='lime-ubuntu', details_url='https://lime/ubuntu')
        self.jackett.add_result('lime', 'public', 'Completely Unrelated Thing', 3, 42_000_000, download_key='lime-other', details_url='https://lime/other')
        self.jackett.add_result('1337x', 'public', 'Some File mkv', 10, TORRENT_SIZE, download_key='x-fail', details_url='https://1337x/some')
        self.jackett.add_result('tdl', 'public', 'Some.File.mkv', 20, TORRENT_SIZE, download_key='tdl-some', details_url='https://tdl/some')
        self.jackett.add_result('priv', 'private', 'Ubuntu 26.04 Desktop amd64', 999, 6 * GiB, download_key='priv-ubuntu', details_url='https://priv/ubuntu')
        rows, seconds = self.run_search(fold_lookalikes=False, debug_log=True, lookup_remaining_hashes=False)
        self.assertEqual(sorted(self.jackett.requested_keys()), ['lime-ubuntu', 'tdl-some', 'x-fail'])
        ubuntu = [row for row in rows if infohash in row['link']]
        self.assertEqual(len(ubuntu), 1)
        self.assertIn('[tpb, lime]', ubuntu[0]['name'])
        self.assertIn('udp://r.example:1/announce', self.trackers(ubuntu[0]))
        some_file = [row for row in rows if TORRENT_HASH in row['link']]
        self.assertEqual(len(some_file), 1)
        self.assertIn('tdl', some_file[0]['name'])
        # the trackers come from the .torrent
        self.assertEqual(self.trackers(some_file[0]), {'udp://tr.example:6969', 'udp://tier2.example:1'})
        self.assertEqual(len([row for row in rows if 'x-fail' in row['link']]), 1)
        self.assertEqual(len([row for row in rows if 'priv-ubuntu' in row['link']]), 1)
        self.assertEqual(len(rows), 5)
        # everything resolved early, so no waiting for show_results_after
        self.assertLess(seconds, 2.0)
        self.assertIn('lookups=3', self.log_text())
        self.assertIn('lookup failed', self.log_text())

    # Three spellings and three rounded sizes of one release across four indexers. A details
    # page listed twice is fetched once; a same-name re-upload with another hash stays its
    # own row; a row with no size qualifies on title alone.
    def test_one_lookup_per_details_page(self):
        infohash = 'f' * 40
        other_hash = 'e' * 40
        self.jackett.add_result('tpb', 'public', 'Name 2018 720p BRRip Bengali Dub x264 1XBET', 40, int(1.149 * GiB), infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.download_replies = {'x-111': ('magnet', infohash), 'x-222': ('magnet', other_hash), 'lime-333': ('magnet', infohash), 'tdl-444': ('magnet', infohash)}
        page_111 = 'https://1337x.to/torrent/111/'
        self.jackett.add_result('1337x', 'public', 'Name (2018) 720p BDRip [Bengali-Dub] x264 - 1XBET', 30, int(1.2 * GiB), download_key='x-111', details_url=page_111)
        self.jackett.add_result('1337x', 'public', 'Name (2018) 720p BDRip [Bengali-Dub] x264 - 1XBET', 29, int(1.2 * GiB), download_key='x-111', details_url=page_111)
        self.jackett.add_result('1337x', 'public', 'Name 2018 720p BRRip Bengali Dub x264-1XBET', 20, int(1.2 * GiB), download_key='x-222', details_url='https://1337x.to/torrent/222/')
        self.jackett.add_result('lime', 'public', 'Name 2018 720p BRRip Bengali Dub x264-1XBET', 10, int(1.1 * GiB), download_key='lime-333', details_url='https://lime/333')
        self.jackett.add_result('tdl', 'public', 'Name 2018 720p BRRip Bengali Dub x264 1XBET', 5, None, download_key='tdl-444', details_url='https://tdl/444')
        rows, _ = self.run_search(fold_lookalikes=False)
        self.assertEqual(self.jackett.requested_keys().count('x-111'), 1)
        self.assertEqual(len(self.jackett.requested_keys()), 4)
        self.assertEqual(len(rows), 2)
        merged = next(row for row in rows if infohash in row['link'])
        self.assertTrue(merged['name'].endswith('[tpb, 1337x +2]'), merged['name'])
        self.assertTrue(any(other_hash in row['link'] for row in rows))

    # A row whose site reported no size takes the size from the .torrent its lookup
    # returns, and so becomes foldable: with folding on it folds behind the better-seeded
    # same-title row.
    def test_unsized_row_gets_size_from_torrent(self):
        other_hash = 'a' * 40
        self.jackett.add_result('tpb', 'public', 'Some File mkv', 50, TORRENT_SIZE, infohash=other_hash, magnet=f'magnet:?xt=urn:btih:{other_hash}')
        self.jackett.download_replies = {'lime-some': ('torrent', TORRENT_FILE)}
        self.jackett.add_result('lime', 'public', 'Some.File.mkv', 10, None, download_key='lime-some', details_url='https://lime/some')
        rows, _ = self.run_search(fold_lookalikes=False)
        self.assertEqual(len(rows), 2)
        # a lone .torrent-resolved row keeps its .torrent link, so it is found by that
        from_torrent = next(row for row in rows if 'lime-some' in row['link'])
        self.assertEqual(from_torrent['size'], TORRENT_SIZE)
        rows, _ = self.run_search()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['name'].endswith('[tpb] +1 alt'), rows[0]['name'])

    # The second round: after the likely duplicates, the remaining hash-less rows are looked
    # up too. A DHT site's file name that looks nothing like the release name still merges
    # once its hash matches, and the merged row carries the seed count the DHT site lacked;
    # a row whose hash matches nothing is shown on its own with the hash it got. With
    # lookup_per_indexer 1 the likely duplicate on a site is fetched before the second-round
    # row on the same site.
    def test_remaining_hashes_looked_up_after_likely_duplicates(self):
        infohash = 'a' * 40
        self.jackett.add_result('tpb', 'public', 'Alpha Movie 2019 1080p WEB-DL', 50, GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.download_replies = {'lime-alpha': ('magnet', infohash), 'lime-else': ('magnet', 'b' * 40), 'dht-file': ('magnet', infohash)}
        self.jackett.add_result('lime', 'public', 'Alpha Movie 2019 1080p WEB-DL', 20, GiB, download_key='lime-alpha', details_url='https://lime/alpha')
        self.jackett.add_result('lime', 'public', 'Something Else Entirely', 5, 2 * GiB, download_key='lime-else', details_url='https://lime/else')
        self.jackett.add_result('dht', 'public', 'am.2019.1080p.x264.mkv', -1, GiB + 50_000_000, download_key='dht-file', details_url='https://dht/file')
        rows, _ = self.run_search(fold_lookalikes=False, lookup_per_indexer=1, debug_log=True)
        keys = self.jackett.requested_keys()
        self.assertEqual(sorted(keys), ['dht-file', 'lime-alpha', 'lime-else'])
        self.assertLess(keys.index('lime-alpha'), keys.index('lime-else'))
        self.assertEqual(len(rows), 2)
        merged = next(row for row in rows if infohash in row['link'])
        self.assertTrue(merged['name'].startswith('Alpha Movie 2019 1080p WEB-DL [tpb, lime +1]'), merged['name'])
        self.assertEqual(merged['seeds'], 50)
        self.assertTrue(any('b' * 40 in row['link'] for row in rows))
        self.assertIn('second_round_lookups=2 second_round_matched=1', self.log_text())

    # Off: only the likely duplicate is looked up; the other two rows stay as they are.
    def test_remaining_hashes_off(self):
        infohash = 'a' * 40
        self.jackett.add_result('tpb', 'public', 'Alpha Movie 2019 1080p WEB-DL', 50, GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.download_replies = {'lime-alpha': ('magnet', infohash), 'lime-else': ('magnet', 'b' * 40), 'dht-file': ('magnet', infohash)}
        self.jackett.add_result('lime', 'public', 'Alpha Movie 2019 1080p WEB-DL', 20, GiB, download_key='lime-alpha', details_url='https://lime/alpha')
        self.jackett.add_result('lime', 'public', 'Something Else Entirely', 5, 2 * GiB, download_key='lime-else', details_url='https://lime/else')
        self.jackett.add_result('dht', 'public', 'am.2019.1080p.x264.mkv', -1, GiB + 50_000_000, download_key='dht-file', details_url='https://dht/file')
        rows, _ = self.run_search(fold_lookalikes=False, lookup_remaining_hashes=False)
        self.assertEqual(self.jackett.requested_keys(), ['lime-alpha'])
        self.assertEqual(len(rows), 3)

    # The second round ends at the first showing: a slow second-round lookup does not hold
    # its row or the search; the row is shown as it is, and a copy of it that arrives after
    # the lookup finished is dropped.
    def test_second_round_ends_at_first_showing(self):
        infohash = 'a' * 40
        self.jackett.add_result('tpb', 'public', 'Alpha Movie 2019 1080p WEB-DL', 50, GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.download_replies = {'dht-slow': ('sleep', 3.0, 'c' * 40)}
        self.jackett.add_result('dht', 'public', 'unrelated.file.name.mkv', -1, 3 * GiB, download_key='dht-slow', details_url='https://dht/slow')
        rows, seconds = self.run_search(fold_lookalikes=False)
        self.assertEqual(len(rows), 2)
        unresolved = next(row for row in rows if 'dht-slow' in row['link'])
        self.assertTrue(1.9 <= unresolved['at'] <= 2.6, unresolved['at'])
        self.assertLess(seconds, 2.7)

    # A second-round row whose lookup is already running is moved to the first round when a
    # twin arrives: it is held until its hash is known, and as that hash is on screen by then
    # (the twin was shown at the first showing) the row is dropped instead of shown as a
    # duplicate. The search ends as soon as the lookup is in, not at max_search_time.
    def test_running_second_round_lookup_is_held_when_twin_arrives(self):
        infohash = 'a' * 40
        self.jackett.download_replies = {'lime-alpha': ('sleep', 3.0, infohash)}
        self.jackett.add_result('lime', 'public', 'Alpha Movie 2019 1080p WEB-DL', 20, GiB, download_key='lime-alpha', details_url='https://lime/alpha')
        self.jackett.add_result('tpb', 'public', 'Alpha Movie 2019 1080p WEB-DL', 50, GiB, infohash=infohash, magnet=f'magnet:?xt=urn:btih:{infohash}')
        self.jackett.search_delays['tpb'] = 1.0
        rows, seconds = self.run_search(fold_lookalikes=False)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['name'].startswith('Alpha Movie 2019 1080p WEB-DL [tpb]'), rows[0]['name'])
        self.assertLess(seconds, 5.0)

    # A fast lookup merges before the first batch; one finishing later for a hash already
    # shown is dropped; one that never finishes keeps the search alive until
    # max_search_time, when its row is shown as-is.
    def test_deadlines(self):
        hashes = ['1' * 40, '2' * 40, '3' * 40, '4' * 40]
        for episode, infohash in enumerate(hashes, start=1):
            self.jackett.add_result('tpb', 'public', f'Show S01E0{episode} 1080p', 101 - episode, 1000 * episode, infohash=infohash,
                                    magnet=f'magnet:?xt=urn:btih:{infohash}&dn=x')
        self.jackett.download_replies = {'fast': ('magnet', hashes[0]), 'late': ('sleep', 3.0, hashes[1]),
                                         'never': ('sleep', 30.0, hashes[2]), 'fast-too': ('magnet', hashes[3])}
        self.jackett.add_result('lime', 'public', 'Show S01E01 1080p', 60, 1000, download_key='fast', details_url='p1')
        self.jackett.add_result('lime', 'public', 'Show S01E02 1080p', 50, 2000, download_key='late', details_url='p2')
        self.jackett.add_result('lime', 'public', 'Show S01E03 1080p', 40, 3000, download_key='never', details_url='p3')
        self.jackett.add_result('lime', 'public', 'Show S01E04 1080p', 30, 4000, download_key='fast-too', details_url='p4')
        rows, seconds = self.run_search(fold_lookalikes=False, max_search_time=4)
        by_hash = {infohash: [row for row in rows if infohash in row['link']] for infohash in hashes}
        for infohash in (hashes[0], hashes[3]):
            self.assertEqual(len(by_hash[infohash]), 1)
            self.assertIn('[tpb, lime]', by_hash[infohash][0]['name'])
            self.assertLess(by_hash[infohash][0]['at'], 2.6)
        self.assertEqual(len(by_hash[hashes[1]]), 1)
        self.assertIn('[tpb]', by_hash[hashes[1]][0]['name'])
        never = [row for row in rows if 'path=never' in row['link']]
        self.assertEqual(len(never), 1)
        self.assertTrue(3.8 <= never[0]['at'] <= 5.0, never[0]['at'])
        self.assertTrue(3.8 <= seconds <= 5.0, seconds)
        self.assertEqual(len(rows), 5)


class LookalikeFolding(SearchTest):
    # A [TGx] re-upload, a no-size twin, the YTS BRRip/WEBRip pair, two Russian encodes, a
    # semi-private copy, and two lookalikes arriving late from a slow indexer (one weaker,
    # one stronger).
    def add_lookalike_results(self):
        add = self.jackett.add_result
        add('tpb', 'public', 'Name 2018 720p WEB-DL-1XBET', 120, int(1.3 * GiB), infohash=fake_hash('clean'), magnet=f'magnet:?xt=urn:btih:{fake_hash("clean")}')
        add('tpb', 'public', 'Name 2018 720p WEB-DL-1XBET[TGx]', 80, int(1.3 * GiB) + 1234, infohash=fake_hash('tgx'), magnet=f'magnet:?xt=urn:btih:{fake_hash("tgx")}')
        add('tpb', 'public', 'Name 2018 720p WEB-DL-1XBET', 7, None, infohash=fake_hash('nosize'), magnet=f'magnet:?xt=urn:btih:{fake_hash("nosize")}')
        add('yts', 'public', 'Name (2018) 720p BRRip x264 -YTS', 500, int(0.95 * GiB), infohash=fake_hash('brrip'), magnet=f'magnet:?xt=urn:btih:{fake_hash("brrip")}')
        add('yts', 'public', 'Name (2018) 720p WEBRip x264 -YTS', 450, int(0.95 * GiB), infohash=fake_hash('webrip'), magnet=f'magnet:?xt=urn:btih:{fake_hash("webrip")}')
        add('knaben', 'public', 'Name [2018, США, комедия, драма, биография, WEB-DL-AVC] Dub (iTunes)', 60, int(2.1 * GiB), infohash=fake_hash('webdl-ru'), magnet=f'magnet:?xt=urn:btih:{fake_hash("webdl-ru")}')
        add('knaben', 'public', 'Name[2018, США, драма, комедия, биография, BDRip] Dub', 55, int(2.1 * GiB), infohash=fake_hash('bdrip-ru'), magnet=f'magnet:?xt=urn:btih:{fake_hash("bdrip-ru")}')
        add('bfg', 'public', 'Name (2018) BDRip-Лицензия', 30, int(2.1 * GiB), infohash=fake_hash('bfg'), magnet=f'magnet:?xt=urn:btih:{fake_hash("bfg")}')
        add('rutracker', 'semi-private', 'Name 2018 720p WEB-DL-1XBET', 20, int(1.3 * GiB), infohash=fake_hash('rutracker'), magnet=f'magnet:?xt=urn:btih:{fake_hash("rutracker")}')
        add('slow', 'public', 'Name 2018 720p WEB-DL-1XBET [rarbg]', 50, int(1.3 * GiB), infohash=fake_hash('late-weak'), magnet=f'magnet:?xt=urn:btih:{fake_hash("late-weak")}')
        add('slow', 'public', 'www.Site.com - Name 2018 720p WEB-DL-1XBET', 900, int(1.3 * GiB), infohash=fake_hash('late-strong'), magnet=f'magnet:?xt=urn:btih:{fake_hash("late-strong")}')
        self.jackett.search_delays['slow'] = 2.5

    # Identical cleaned titles fold; titles that differ by a real word, no-size rows and
    # semi-private rows stay; a late weaker lookalike is hidden, a late stronger one shown.
    def test_folds_identical_titles_only(self):
        self.add_lookalike_results()
        rows, _ = self.run_search(show_results_after=1)
        names = [row['name'] for row in rows]
        self.assertIn('Name 2018 720p WEB-DL-1XBET [tpb] +1 alt', names)
        self.assertFalse(any('[TGx]' in name for name in names))
        self.assertEqual(sum('WEB-DL-1XBET [tpb]' in name for name in names), 2)
        self.assertTrue(any('BRRip' in name for name in names) and any('WEBRip' in name for name in names))
        self.assertEqual(sum('[knaben]' in name for name in names), 2)
        self.assertTrue(any('[bfg]' in name for name in names))
        self.assertTrue(any('[rutracker]' in name for name in names))
        self.assertFalse(any('[rarbg]' in name for name in names))
        self.assertTrue(any(name.startswith('www.Site.com') for name in names))
        self.assertEqual(len(rows), 9)

    # Titles that differ only in non-Latin words, or only in word order ("2.1" vs "1.2"),
    # are different titles and never fold.
    def test_fold_respects_script_and_word_order(self):
        add = self.jackett.add_result
        add('knaben', 'public', 'Name [2018, комедия, BDRip] Dub', 60, 2 * GiB, infohash=fake_hash('ru-1'), magnet=f'magnet:?xt=urn:btih:{fake_hash("ru-1")}')
        add('bfg', 'public', 'Name [2018, драма, BDRip] Dub', 50, 2 * GiB, infohash=fake_hash('ru-2'), magnet=f'magnet:?xt=urn:btih:{fake_hash("ru-2")}')
        add('tpb', 'public', 'Tool 2.1 x64', 40, 1000, infohash=fake_hash('v21'), magnet=f'magnet:?xt=urn:btih:{fake_hash("v21")}')
        add('1337x', 'public', 'Tool 1.2 x64', 30, 1000, infohash=fake_hash('v12'), magnet=f'magnet:?xt=urn:btih:{fake_hash("v12")}')
        rows, _ = self.run_search(show_results_after=1)
        self.assertEqual(len(rows), 4)
        self.assertFalse(any('alt' in row['name'] for row in rows))

    # fold_lookalikes off shows every hash.
    def test_fold_off(self):
        self.add_lookalike_results()
        rows, _ = self.run_search(show_results_after=1, fold_lookalikes=False)
        self.assertEqual(len(rows), 11)
        self.assertFalse(any('+1 alt' in row['name'] for row in rows))

    # The MB floor applies from a gibibyte up: 1.1 GiB and 1.2 GiB with the same title fold
    # (display rounding), 700 MB and 800 MB with the same title are two files and stay two
    # rows.
    def test_size_floor_only_from_a_gibibyte(self):
        add = self.jackett.add_result
        add('tpb', 'public', 'Big 2018 1080p', 100, int(1.2 * GiB), infohash=fake_hash('big-a'), magnet=f'magnet:?xt=urn:btih:{fake_hash("big-a")}')
        add('1337x', 'public', 'Big 2018 1080p', 50, int(1.1 * GiB), infohash=fake_hash('big-b'), magnet=f'magnet:?xt=urn:btih:{fake_hash("big-b")}')
        add('tpb', 'public', 'Small 2018 720p', 100, 800 * 1024 * 1024, infohash=fake_hash('small-a'), magnet=f'magnet:?xt=urn:btih:{fake_hash("small-a")}')
        add('1337x', 'public', 'Small 2018 720p', 50, 700 * 1024 * 1024, infohash=fake_hash('small-b'), magnet=f'magnet:?xt=urn:btih:{fake_hash("small-b")}')
        rows, _ = self.run_search(show_results_after=1)
        names = [row['name'] for row in rows]
        self.assertEqual(len(rows), 3, names)
        self.assertIn('Big 2018 1080p [tpb] +1 alt', names)
        self.assertEqual(sum('Small 2018 720p' in name for name in names), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
