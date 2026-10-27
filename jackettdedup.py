#VERSION: 2.12
# AUTHORS: Arihan Jalan
# CREDITS: the parts that talk to Jackett and qBittorrent (API calls, category ids, error
#          row, User-Agent, proxy handling) follow jackett.py 4.14 by Diego de las Heras
#          (ngosang@hotmail.es) and contributors, GPL-2.0. The merging is new.
#
# Copyright (C) 2026 Arihan Jalan
#
# This program is free software; you can redistribute it and/or modify it under the terms of
# the GNU General Public License version 2 as published by the Free Software Foundation.
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details: <https://www.gnu.org/licenses/>.
#
# qBittorrent search plugin for Jackett.
#
# The same torrent is often uploaded to several torrent sites, so a Jackett search lists it
# once per site: one torrent, five rows. This plugin collapses those rows into one, with the
# trackers and the best seed count from every site. It also hides rows that are the same
# files uploaded again as a new torrent behind the best-seeded one.
#
# When searching with this plugin, it:
#   1. Searches every site Jackett is set up with, all in parallel.
#   2. Merges rows with the same infohash (the id of a torrent's contents) into one row:
#      the public sites' trackers go into one magnet link (a private site's copy as
#      use_private_or_public_tracker says), the highest seed count is shown and a
#      "[TPB, 1337x +2]" suffix names the sites.
#   3. Some sites (1337x, LimeTorrents, ...) report no infohash. When such a row looks like
#      a row from another site (similar title and size), its infohash is fetched: the row's
#      download link on Jackett returns a magnet or a .torrent file, and both contain it.
#      The row is then merged like any other. The remaining hash-less rows are looked up
#      too while there is time before the first showing: a copy whose title looks nothing
#      like the others still merges, and a row from a site that reports no seeders gets
#      the seed count of the copy that does.
#   4. Rows with the same cleaned title and size but a different infohash are different
#      torrents, usually the same files uploaded twice:
#        Name 2018 720p WEB-DL-1XBET
#        Name 2018 720p WEB-DL-1XBET[TGx]
#      They are not merged, since that would mix two swarms; the best-seeded one is shown
#      with a "+1 alt" suffix and the other is hidden.
#   5. Shows the results after show_results_after seconds; rows from slow sites follow.
#
# Code map: the module-level sections, then the methods of class jackettdedup in the order
# one search runs them.
#   SETTINGS       every setting with its default; _load_settings reads jackettdedup.json
#   INFOHASHES     reading an infohash, trackers and size out of a magnet link or a
#                  .torrent file
#   TITLES         how two rows are compared: title words and sizes
#   HELPERS        XML and proxy helpers
#   search                  runs one search: starts the site searches, then every 250 ms
#                           starts lookups and shows the rows that are ready, until all is
#                           done or time is up
#   _configured_indexers    gets the list of sites Jackett is set up with
#   _search_indexer         searches one site, in its own thread, and adds its rows; tries
#                           again when the site fails (search_retries)
#   _row_from_item          reads one result in Jackett's reply into the row dict the rest
#                           of the file works on
#   _start_lookups          every 250 ms: starts a hash lookup, in a lookup thread, for each
#                           hash-less row that _looks_like_duplicate approves, then it looks
#                           up other hash-less rows on the offchance they match a torrent
#                           despite having different names
#   _looks_like_duplicate   whether a hash-less row resembles a row from another site
#                           (similar size and title), so a lookup is worth a page fetch
#   _lookup_hash            one lookup: fetches the row's download link and reads the hash
#   _finish_lookup          stores a finished lookup's hash on the rows waiting for it
#   _show_ready_rows        every 250 ms: groups the unshown rows by hash and shows the
#                           groups that are ready
#   _merged_row             builds the one result row for a group of rows with the same
#                           hash: title, seed count, site suffix, link
#   _merged_link            picks that row's link: the public copies' pooled magnet, a
#                           private copy's .torrent, or both in one magnet, as
#                           use_private_or_public_tracker says
#   _merged_magnet          builds a pooled magnet link: the hash plus the given copies' trackers
#   _fold_lookalikes        hides rows with the same title and size as a better-seeded row
#                           ("+N alt")
#   _show_result            prints one row to qBittorrent
#   _append_log             writes the debug log once the search is over
# Later, when the user downloads a row:
#   download_torrent        fetches the row's download link from Jackett and hands
#                           qBittorrent the magnet or the .torrent file

import base64
import bisect
import datetime
import gzip
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.cookiejar import CookieJar
from multiprocessing.dummy import Pool
from threading import Lock
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import helpers
from novaprinter import prettyPrinter


# =========================================================================== SETTINGS
# The settings live in jackettdedup.json in qBittorrent's plugin folder (nova3/engines),
# next to the copy of this file that qBittorrent made when the plugin was installed; not
# next to the downloaded .py. The file is created, with every default below, when the
# plugin is installed, and read again at the start of every search, so edits need no
# restart. Each setting is explained above its default. Jackett calls a site an "indexer";
# the setting names do too.

_PLUGIN_DIR = os.path.dirname(os.path.realpath(__file__))
_SETTINGS_PATH = os.path.join(_PLUGIN_DIR, 'jackettdedup.json')
# the official Jackett plugin's settings, copied on the first run
_OFFICIAL_SETTINGS_PATH = os.path.join(_PLUGIN_DIR, 'jackett.json')
_LOG_PATH = os.path.join(_PLUGIN_DIR, 'jackettdedup.log')

_DEFAULTS: Dict[str, Any] = {

    # ---- connection ------------------------------------------------------------------
    # Optional when the official Jackett plugin is set up: api_key, url, concurrent_searches
    # and indexers_before_title (under display) are copied from its jackett.json the first
    # time this plugin runs.

    # Jackett's API key (dashboard, top right).
    'api_key': 'YOUR_API_KEY_HERE',

    # Jackett's address. Jackett on another machine: "http://192.168.1.10:9117". The one
    # setting that needs a qBittorrent restart: qBittorrent remembers the plugin's address
    # from its own start and matches result rows against it (README, Known limitations).
    'url': 'http://127.0.0.1:9117',

    # Sites searched at the same time. 1 = one combined request to Jackett, which answers
    # only when the slowest site has. Range 1-50.
    'concurrent_searches': 20,

    # Seconds before a request to Jackett is given up, searches and hash lookups alike.
    # Sites behind FlareSolverr can take 20-30 s. Range 15-120.
    'request_timeout': 45,

    # Extra attempts when a site's search fails (Jackett reports an error for it, or cannot
    # be reached): a Cloudflare challenge or a site hiccup often clears on the next try,
    # the way a red Test button in Jackett turns green when pressed again. Each extra
    # attempt waits 2 seconds first; none is made for a rate limit (HTTP 429) or when the
    # search is nearly out of time. 0 = report the failure at once. Range 0-5.
    'search_retries': 3,

    # ---- display ---------------------------------------------------------------------

    # Add "[TPB, 1337x +2]" to each row: the sites it was found on.
    'show_indexers': True,

    # Site names written out before "+N". Range 1-5.
    'max_indexers_shown': 2,

    # "[TPB, 1337x] Title" instead of "Title [TPB, 1337x]". Handy when sorting by name.
    'indexers_before_title': False,

    # A torrent found on both private (or semi-private) and public sites: which link the
    # merged row gets. Private sites never have their hashes looked up; this only decides
    # the link of a row they share with public sites. A row with public copies only gets
    # the pooled public magnet; a row with private copies only links the best-seeded one's
    # .torrent file.
    #   public    a magnet with the public trackers only; the private copy adds its seed
    #             count and site name, nothing else. No ratio credit on the private site.
    #   private   the private site's .torrent file: carries your passkey, counts toward
    #             your ratio, and follows the tracker's rules whatever the torrent is.
    #   both      one magnet with the private site's trackers and the public ones; when
    #             the private site gives no magnet, its .torrent file instead.
    #   highest   whichever copy has the most seeders decides, and only that side is used:
    #             its .torrent when private, a public magnet otherwise.
    # Mind the private flag: a torrent carrying it stays flagged wherever it is reposted,
    # since the flag sits inside the hashed part; qBittorrent then turns DHT and peer
    # exchange off for it, and announcing it to public trackers breaks the private
    # tracker's rules. "both" does that for such a torrent, and "highest" does when the
    # public copy wins. "public" and "private" never mix the two sides: "private"
    # announces only to the private tracker, "public" only to the public site's trackers,
    # with your private account never involved.
    # Anything else counts as public.
    'use_private_or_public_tracker': 'public',

    # ---- timing, in seconds ----------------------------------------------------------
    # Sites answer and hash lookups finish in the background. qBittorrent lets a plugin add
    # rows but never change or remove one, so when a row is shown decides what it holds:
    #   - At show_results_after every row in hand is shown, except rows with a hash lookup
    #     queued or running: those stay hidden until their lookup finishes, so that a copy
    #     of a torrent already on screen is dropped instead of shown twice (most are).
    #   - A copy that arrives after its torrent's row was shown (a slow site, a late lookup)
    #     is dropped: it cannot raise that row's seed count or add its trackers any more.
    #   - At max_search_time the search ends: rows still waiting for a lookup are shown
    #     unmerged, and whatever is still running is abandoned.

    # Seconds before the first rows are shown. Lower = faster, more duplicates (copies that
    # arrive later are dropped, not merged). 10 works without FlareSolverr. Range 5-60.
    'show_results_after': 20,

    # Seconds after which the search ends, whatever is still running. Raise it when the log
    # shows "lookup still running at max_search_time". Range 30-120.
    'max_search_time': 60,

    # ---- hash lookups ----------------------------------------------------------------
    # Some sites (1337x, LimeTorrents, TorrentDownloads, Torrent9, Magnetz, ...) report no
    # infohash, so their rows cannot be merged as they arrive. For such a row the plugin
    # fetches its download link from Jackett, which returns a magnet or a .torrent, and
    # reads the hash from that: one page fetch on the site per row. Rows are looked up in
    # two rounds:
    #   1. Likely duplicates: rows that resemble a row from another site, sizes within
    #      size_tolerance of each other and titles sharing lookup_title_similarity of their
    #      words once brackets, punctuation and site tags are ignored. A row that reports
    #      no size qualifies on its title alone. Held until the lookup finishes, up to
    #      max_search_time.
    #   2. Every other hash-less row (lookup_remaining_hashes), with the time left before
    #      show_results_after. Shown as it is at show_results_after if its lookup has not
    #      finished by then.
    # A row that reports no size takes its size from the .torrent when the lookup returns
    # one. Public sites only (on private ones a download counts against the account), each
    # page at most once per search, nothing kept between searches. Threads:
    # lookup_per_indexer for every public site, up to 64.

    # Off = no lookups: hash-less rows are shown as they are. They can still be folded,
    # never merged.
    'lookup_hashes': True,

    # Lookups running at the same time on one site. 2 for sites behind Cloudflare or
    # FlareSolverr, which refuse or queue parallel requests. At 2 or more, one slot is kept
    # free for a likely duplicate that arrives late (see lookup_remaining_hashes); at 1
    # nothing can be kept free, so such a row waits behind a second-round lookup. Range 1-8.
    'lookup_per_indexer': 4,

    # Round 2: after the likely duplicates, look up the hash of every other hash-less public
    # row too, while there is time before show_results_after. Likely duplicates always go
    # first, and one of each site's lookup_per_indexer slots is kept free for them (at 1
    # there is nothing to keep free). This catches copies whose titles are too different to
    # look alike (a DHT site's file name against a release name), and gives a row from a
    # site that reports no seeders the seed count of the copy that does. Off = likely
    # duplicates only.
    'lookup_remaining_hashes': True,

    # Share of title words two rows must have in common: shared words / all words, 0-1.
    # Brackets and punctuation are not words, so these two share 7 words of 9 = 0.78 (only
    # BDRip and BRRip differ):
    #   Name (2018) 720p BDRip [Bengali-Dub] x264 - 1XBET
    #   Name 2018 720p BRRip Bengali Dub x264-1XBET
    # Range 0.2-0.8.
    'lookup_title_similarity': 0.4,

    # ---- sizes, for lookups and folding ----------------------------------------------
    # Two sizes count as the same when they differ by at most size_tolerance of the larger
    # one, or, when the larger one is at least 1 GiB, by at most size_tolerance_mb,
    # whichever allows more. Below 1 GiB the fraction alone decides: there the MB floor
    # would be far more than display rounding (700 MB and 800 MB are different files).

    # The fraction. Range 0.01-0.10.
    'size_tolerance': 0.05,

    # The MB floor, for sizes of 1 GiB and more. Sites round differently, so "1.2 GB" on
    # one and "1.1 GB" on another can be the same file. 0 = fraction only. Range 0-200.
    'size_tolerance_mb': 110,

    # ---- lookalike folding -----------------------------------------------------------
    # Same cleaned title (identical words, in order), same size, different hash: almost
    # always the same files uploaded twice (renamed folder, extra .txt, a tracker's own keys
    # inside the torrent). The torrents are not combined; the best-seeded row is shown with
    # "+N alt" and the rest are hidden. Titles that differ by one word never fold; these
    # two are different encodes and stay two rows:
    #   Name (2018) 720p BRRip x264 -YTS
    #   Name (2018) 720p WEBRip x264 -YTS
    # Private-tracker rows and rows without a size are never folded.

    # Off = every distinct hash is shown.
    'fold_lookalikes': True,

    # Site tags removed from titles before they are compared, as case-insensitive regexes.
    # A match is blanked out and the title is then split into words, so the space or
    # bracket left behind does not matter; these two
    #   Name 2018 720p WEB-DL-1XBET[TGx]
    #   Name 2018 720p WEB-DL-1XBET
    # both become (name, 2018, 720p, web, dl, 1xbet). Add your own, e.g. "\\[1tamilmv\\]".
    'fold_strip_patterns': [
        r"\[(tgx|torrentgalaxy|rarbg|rartv|ettv|eztv|etrg|publichd|ethd|yify|1337x|kat)\]",
        r"\[yts\.(mx|am|lt|ag|gg)\]",
        r"\bwww\.[a-z0-9-]+\.[a-z]{2,6}\b",
    ],

    # ---- debugging -------------------------------------------------------------------

    # Write jackettdedup.log next to this file: per-search totals, a line per site, and
    # every row that could not be merged with the reason. Grows until deleted.
    'debug_log': False,
}

# the settings file as loaded, every key present
_SETTINGS: Dict[str, Any] = {}
# when set, every search shows this instead of running
_SETTINGS_ERROR = ''
# the official plugin's setting names -> ours
_OFFICIAL_KEYS = {
    'api_key': 'api_key',
    'url': 'url',
    'thread_count': 'concurrent_searches',
    'tracker_first': 'indexers_before_title',
}


# Loads jackettdedup.json into _SETTINGS. Runs when qBittorrent imports the plugin: at
# install, at its own start, and at every search and download, each a new Python process,
# which is why edits to the file apply at the next search.
#   1. The file exists: read it. Unreadable, or not JSON = every search shows an error row.
#   2. It does not exist (first run): start from the official plugin's jackett.json, if any.
#   3. Add every setting the file lacks with its default, drop settings this version no
#      longer has, and write the file back, so it always lists exactly the settings above.
# Sets _SETTINGS_ERROR instead when the file is broken or api_key was never filled in.
def _load_settings() -> None:
    # module state, read by _setting() and search()
    global _SETTINGS, _SETTINGS_ERROR
    loaded: Any = {}
    if os.path.exists(_SETTINGS_PATH):
        # 1. read our file
        try:
            with open(_SETTINGS_PATH, encoding='utf-8') as settings_file:
                loaded = json.load(settings_file)
        # exists but cannot be opened (permissions)
        except OSError:
            _SETTINGS_ERROR = 'configuration file cannot be read'
            return
        # not JSON: a stray comma or quote, usually
        except ValueError:
            _SETTINGS_ERROR = 'malformed configuration file'
            return
        # JSON, but a list or a string, not settings
        if not isinstance(loaded, dict):
            _SETTINGS_ERROR = 'malformed configuration file'
            return
    elif os.path.exists(_OFFICIAL_SETTINGS_PATH):
        # 2. first run: copy the connection settings from the official plugin's file, so a
        #    user who already has it set up fills in nothing. A broken jackett.json is not
        #    this plugin's problem: start from the defaults instead.
        try:
            with open(_OFFICIAL_SETTINGS_PATH, encoding='utf-8') as official_file:
                official = json.load(official_file)
            loaded = {ours: official[theirs] for theirs, ours in _OFFICIAL_KEYS.items() if theirs in official}
        except (OSError, ValueError, TypeError):
            loaded = {}
    # 3. add what the file lacks, drop what this version no longer has, write it back
    missing = [key for key in _DEFAULTS if key not in loaded]
    obsolete = [key for key in loaded if key not in _DEFAULTS]
    for key in missing:
        loaded[key] = _DEFAULTS[key]
    for key in obsolete:
        del loaded[key]
    _SETTINGS = loaded
    if missing or obsolete:
        try:
            with open(_SETTINGS_PATH, 'w', encoding='utf-8') as settings_file:
                settings_file.write(json.dumps(_SETTINGS, indent=4, sort_keys=True))
        # a read-only plugin folder: the settings in memory are complete anyway
        except OSError:
            pass
    if _SETTINGS['api_key'] == _DEFAULTS['api_key']:
        _SETTINGS_ERROR = 'api key error'


# Casts a setting from the file to the type of its default, since the file is edited by
# hand: "20" -> 20, "true" -> True, and "abc" where a number is expected -> the default.
def _setting(key: str) -> Any:
    default = _DEFAULTS[key]
    value = _SETTINGS.get(key, default)
    # booleans first, before the general cast below: bool("false") would be True
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    # fold_strip_patterns: a list is used as it is, anything else is replaced
    if isinstance(default, list):
        return list(value) if isinstance(value, list) else default
    # int("20"), float("0.5"), str(...)
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        return default


_load_settings()


# ========================================================================= INFOHASHES
# The infohash identifies a torrent's contents: same hash = same torrent, mergeable;
# different hash = different torrent, whatever the titles say. A site hands it over in one
# of two forms: inside a magnet link (the urn:btih: part), or it has to be computed from a
# .torrent file.
#
# A .torrent file is "bencoded": a simple binary format with four kinds of value, nested
# inside one dict at the top level:
#   integer  i42e
#   string   5:hello             (length, colon, the bytes)
#   list     l<values>e
#   dict     d<key><value>...e   (keys are strings)
# The infohash is the SHA-1 of the bytes of the top-level `info` value exactly as they are
# in the file, so the helpers below locate those bytes instead of decoding and re-encoding
# them: re-encoding could change a byte, and with it the hash.
#
# That is the BitTorrent v1 infohash, the one sites, Jackett and nearly every torrent use.
# BitTorrent v2 (2020) identifies a torrent by the SHA-256 of the same `info` bytes
# instead, written in a magnet as urn:btmh:1220<64 hex chars> (1220 = "SHA-256, 32
# bytes"). A "hybrid" torrent carries both layouts and keeps its v1 hash as the key here.
# A v2-only torrent (`meta version` 2 in info, no `pieces`) has no v1 hash at all; its key
# is "v2:" + the 64 hex chars, so it merges with other v2-only copies and never collides
# with a v1 key. Such torrents are rare on public sites, which index by v1 hash.

# the hash inside a magnet link: the 32 or 40 chars after urn:btih:
_BTIH_RE = re.compile(r'urn:btih:([0-9a-z]{32,40})', re.IGNORECASE)
# the v2 hash inside a magnet link: the 64 hex chars after urn:btmh:1220
_BTMH_RE = re.compile(r'urn:btmh:1220([0-9a-f]{64})', re.IGNORECASE)
# a hash already written as 40 hex chars (v1) or 64 (v2)
_HEX40_RE = re.compile(r'^[0-9a-f]{40}$')
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


# Converts a hash to the one form rows are grouped by: 40 lowercase hex chars for a v1
# hash, "v2:" + 64 lowercase hex chars for a v2 one. Sites write the same v1 hash as hex in
# either case or as 32 base32 chars, and only one spelling can be the key.
#   returns: the key, or None when raw is not a hash at all
def _normalize_hash(raw: str) -> Optional[str]:
    raw = raw.strip().lower()
    # already 40 hex chars: done
    if _HEX40_RE.match(raw):
        return raw
    # 64 hex chars: a v2 hash
    if _HEX64_RE.match(raw):
        return 'v2:' + raw
    # 32 chars: base32, decode it to hex
    if len(raw) == 32:
        try:
            return base64.b32decode(raw.upper()).hex()
        # 32 chars, but not base32
        except ValueError:
            return None
    return None


# Parses a magnet link for the torrent's infohash: the v1 hash (urn:btih) when there is
# one, else the v2 hash (urn:btmh) of a v2-only torrent.
#   returns: the key, or None when link is not a magnet or has neither part
def _hash_from_magnet(link: str) -> Optional[str]:
    if not link.lower().startswith('magnet:'):
        return None
    match = _BTIH_RE.search(link)
    if match:
        return _normalize_hash(match.group(1))
    match = _BTMH_RE.search(link)
    return 'v2:' + match.group(1).lower() if match else None


# Finds where one bencoded value ends without decoding it. _hash_from_torrent walks the
# .torrent's top-level dict with this to find the exact bytes of the `info` value, which
# it hashes as they are (see the section comment above).
#   data: a .torrent file
#   position: index of the value's first byte, which says what kind of value it is
#   returns: index of the first byte after the value
#   raises: ValueError when the bytes are not bencode: b'<html>...' (an error page: the
#           first byte is none of i, l, d or a digit), or a file cut short
#   example: b'l4:spami42ee' from position 0
#              l         a list: skip the values inside it until its closing e
#              4:spam    a 4-byte string, skipped to position 7
#              i42e      an integer, skipped to position 11
#              e         the list's end -> returns 12
def _bencode_end(data: bytes, position: int) -> int:
    kind = data[position:position + 1]
    # an integer, i42e: it ends at the first e
    if kind == b'i':
        return data.index(b'e', position) + 1
    # a list l...e or dict d...e: skip every value inside it until the closing e
    if kind in (b'l', b'd'):
        position += 1
        while data[position:position + 1] != b'e':
            # a dict holds key, value pairs: skip the key here, the value below
            if kind == b'd':
                position = _bencode_end(data, position)
            position = _bencode_end(data, position)
        return position + 1
    # a string, 5:hello: the number before the colon says how many bytes follow it
    if kind.isdigit():
        colon = data.index(b':', position)
        end = colon + 1 + int(data[position:colon])
        # the string runs past the end of the data: a file cut short
        if end > len(data):
            raise ValueError('bencode cut short')
        return end
    # any other first byte: this is not a .torrent (an HTML error page, say)
    raise ValueError('not bencode')


# Decodes one bencoded value into Python: int, bytes, list or dict. Used for a .torrent's
# small top-level values (announce, announce-list, the file sizes), never for `info` as a
# whole, which must stay raw bytes for hashing; see _hash_from_torrent.
#   returns: (the value, index of the first byte after it)
#   raises: ValueError when the bytes are not bencode (see _bencode_end); TypeError for a
#           dict whose key is not a string
#   example: b'd8:announce16:udp://tr.example4:infod6:lengthi7eee' from position 0
#              d                    a dict: decode key, value pairs until its closing e
#              8:announce           key, an 8-byte string
#              16:udp://tr.example  its value, a 16-byte string
#              4:info               key
#              d6:lengthi7ee        its value, a dict holding length = 7
#              e                    the dict's end
#              -> ({b'announce': b'udp://tr.example', b'info': {b'length': 7}}, 50)
def _bencode_value(data: bytes, position: int) -> Tuple[Any, int]:
    kind = data[position:position + 1]
    # integer i42e: the digits between i and e
    if kind == b'i':
        end = data.index(b'e', position)
        return int(data[position + 1:end]), end + 1
    # list l...e: decode each value until the closing e
    if kind == b'l':
        items, position = [], position + 1
        while data[position:position + 1] != b'e':
            item, position = _bencode_value(data, position)
            items.append(item)
        return items, position + 1
    # dict d...e: decode key, value pairs until the closing e
    if kind == b'd':
        entries, position = {}, position + 1
        while data[position:position + 1] != b'e':
            key, position = _bencode_value(data, position)
            entries[key], position = _bencode_value(data, position)
        return entries, position + 1
    # string 5:hello: the length before the colon, then that many bytes
    if kind.isdigit():
        colon = data.index(b':', position)
        end = colon + 1 + int(data[position:colon])
        if end > len(data):
            raise ValueError('bencode cut short')
        return data[colon + 1:end], end
    raise ValueError('not bencode')


# Finds the raw bytes of a .torrent file's `info` value, the bytes its infohash is computed
# from (see the section comment above).
#   returns: the bytes, or None when data is not a torrent (an HTML error page, say)
def _info_bytes(data: bytes) -> Optional[bytes]:
    # a .torrent is one dict; anything else is not a torrent
    if data[:1] != b'd':
        return None
    try:
        # walk the top-level dict one key, value pair at a time without decoding the
        # values, until the key is `info`
        position = 1
        while data[position:position + 1] != b'e':
            key_end = _bencode_end(data, position)
            key = data[data.index(b':', position) + 1:key_end]
            value_end = _bencode_end(data, key_end)
            if key == b'info':
                return data[key_end:value_end]
            position = value_end
    # the bytes stopped being bencode part-way: cut short, or not a torrent
    except ValueError:
        return None
    # a dict without `info`: not a torrent either
    return None


# Computes the key a .torrent file is merged by: the v1 infohash, the SHA-1 of its raw
# `info` bytes, for a v1 or hybrid torrent; "v2:" + the SHA-256 of those bytes for a
# v2-only torrent, which has no v1 hash.
#   returns: the key, or None when data is not a torrent (an HTML error page, say)
def _hash_from_torrent(data: bytes) -> Optional[str]:
    info = _info_bytes(data)
    if info is None:
        return None
    # v2-only: `meta version` 2 and no v1 `pieces`; a hybrid has both and keeps its v1 hash
    try:
        info_dict = _bencode_value(info, 0)[0]
    # the info value is not a dict's worth of bencode: not a torrent
    except (ValueError, TypeError):
        return None
    if isinstance(info_dict, dict) and info_dict.get(b'meta version') == 2 and b'pieces' not in info_dict:
        return 'v2:' + hashlib.sha256(info).hexdigest()
    return hashlib.sha1(info).hexdigest()


# Decodes a .torrent file for the small values the plugin reads out of it: the trackers
# and the file sizes.
#   returns: the top-level dict, or None when data is not a torrent
def _torrent_dict(data: bytes) -> Optional[Dict[bytes, Any]]:
    try:
        torrent = _bencode_value(data, 0)[0]
    # not bencode
    except (ValueError, TypeError):
        return None
    return torrent if isinstance(torrent, dict) else None


# Reads the tracker URLs a torrent announces to, so a looked-up .torrent adds its trackers
# to the merged magnet the way a magnet's "tr" parameters do.
#   torrent: from _torrent_dict
#   returns: `announce` then every URL of `announce-list`, deduped, in file order; [] when
#            the torrent has none
def _trackers_from_torrent(torrent: Dict[bytes, Any]) -> List[str]:
    candidates = [torrent.get(b'announce')]
    # announce-list is a list of tiers, each a list of URLs; some files flatten it to URLs
    for tier in torrent.get(b'announce-list') or []:
        candidates += list(tier) if isinstance(tier, list) else [tier]
    trackers: List[str] = []
    # keep each URL once; the isinstance check skips a missing `announce` (None) and
    # malformed entries
    for candidate in candidates:
        if isinstance(candidate, bytes):
            url = candidate.decode('utf-8', 'replace').strip()
            if url and url not in trackers:
                trackers.append(url)
    return trackers


# Reads the total size of a torrent's files, for a row whose site reported no size. A
# single-file v1 torrent has info.length; a multi-file one lists info.files, each with a
# length; a v2-only torrent has neither and lists its files in info.`file tree` instead.
#   torrent: from _torrent_dict
#   returns: bytes; 0 when the torrent has no sizes
def _size_from_torrent(torrent: Dict[bytes, Any]) -> int:
    info = torrent.get(b'info')
    if not isinstance(info, dict):
        return 0
    if isinstance(info.get(b'length'), int):
        return max(0, info[b'length'])
    files = info.get(b'files')
    if isinstance(files, list):
        return sum(entry[b'length'] for entry in files if isinstance(entry, dict) and isinstance(entry.get(b'length'), int))
    tree = info.get(b'file tree')
    return _size_from_file_tree(tree) if isinstance(tree, dict) else 0


# Adds up the file lengths in a v2 `file tree`: a dict of names, where a folder is another
# such dict and a file is a dict whose b'' entry holds its length, nested to any depth.
#   example: {b'pack': {b'a.mkv': {b'': {b'length': 10}}, b'b.srt': {b'': {b'length': 5}}}}
#            -> 15
#   returns: bytes
def _size_from_file_tree(tree: Dict[bytes, Any]) -> int:
    total = 0
    for entry in tree.values():
        if not isinstance(entry, dict):
            continue
        leaf = entry.get(b'')
        # a file: the b'' entry carries its length; anything else is a folder to descend
        if isinstance(leaf, dict):
            if isinstance(leaf.get(b'length'), int):
                total += max(0, leaf[b'length'])
        else:
            total += _size_from_file_tree(entry)
    return total


# ============================================================================= TITLES
# Rows are compared by the words of their titles and by size. The words are what is left
# after lowercasing the title, blanking out site tags and splitting on anything that is
# not a letter or digit, so punctuation, brackets and spacing differences between sites do
# not matter.

# a run of letters or digits, in any script (Cyrillic too)
_WORD_RE = re.compile(r'[^\W_]+')


# Splits a title into its words, lowercased, in order.
#   strip_patterns: compiled regexes blanked out first (fold_strip_patterns)
#   returns: e.g. "Name (2018) [Bengali-Dub] x264 - 1XBET[TGx]" -> (name, 2018, bengali, dub, x264, 1xbet)
def _title_words(title: str, strip_patterns: List[Any]) -> Tuple[str, ...]:
    cleaned = title.lower()
    for pattern in strip_patterns:
        cleaned = pattern.sub(' ', cleaned)
    return tuple(_WORD_RE.findall(cleaned))


# Measures how much two titles' words overlap: shared words / all words, order ignored.
#   returns: 0..1; 0 when either title has no words
def _word_overlap(words: Tuple[str, ...], other_words: Tuple[str, ...]) -> float:
    shared = set(words) & set(other_words)
    together = set(words) | set(other_words)
    return len(shared) / len(together) if together else 0.0


# a gibibyte: the size from which display rounding is coarse enough for the MB floor
_GIB = 1024 ** 3


# Decides whether two sizes count as the same: within tolerance (a fraction of the larger
# one) or, for sizes of a gibibyte and more, within abs_tolerance bytes, whichever allows
# more. The bytes allowance covers sites that round differently, "1.2 GB" on one and
# "1.1 GB" on another; below a gibibyte it would be far more than rounding (700 MB and
# 800 MB are different files), so there the fraction alone decides.
def _sizes_match(size: int, other_size: int, tolerance: float, abs_tolerance: int) -> bool:
    larger = max(size, other_size)
    allowance = tolerance * larger
    if larger >= _GIB:
        allowance = max(allowance, abs_tolerance)
    return abs(size - other_size) <= allowance


# Splits a magnet link into its query parameters.
#   returns: {'xt': [...], 'dn': [...], 'tr': [...], ...}; {} when it has none
def _magnet_params(link: str) -> Dict[str, List[str]]:
    return parse_qs(link.split('?', 1)[1] if '?' in link else '', keep_blank_values=False)


# Builds a browser-like User-Agent whose Firefox version grows with the date, so it never
# looks years old to a site. Same scheme as the official plugin.
def _user_agent() -> str:
    version = 125 + (datetime.datetime.now(datetime.timezone.utc).date() - datetime.date(2024, 4, 16)).days // 30
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) Gecko/20100101 Firefox/{version}.0"


# ============================================================================ HELPERS

# XML namespace of the per-result attributes in Jackett's replies
_TORZNAB_NS = 'http://torznab.com/schemas/2015/feed'
# one row printed at a time, whichever thread prints
_PRINT_LOCK = Lock()


# Reads the text of a child element of an XML element; '' when there is none.
def _child_text(element: Any, tag: str) -> str:
    child = element.find(tag)
    return (child.text or '').strip() if child is not None else ''


# Reads one of a result's <torznab:attr name="..." value="..."/> attributes (seeders,
# peers, infohash, magneturl); '' when the result has none by that name.
def _torznab_attr(item: Any, name: str) -> str:
    child = item.find(f'./{{{_TORZNAB_NS}}}attr[@name="{name}"]')
    return child.attrib.get('value', '') if child is not None else ''


# Describes an HTTP error from Jackett in one line, for an error row and the log. A
# search that fails inside Jackett (the site is down, a Cloudflare challenge, a timeout)
# comes back as HTTP 400, or 429 for a rate limit, with an <error code="900"
# description="..."/> body whose description starts with the site's own error message.
#   returns: e.g. "HTTP 400: Exception (1337x): Cloudflare challenge failed"; just
#            "HTTP 500" when the body carries no message
def _jackett_error(error: urllib.error.HTTPError) -> str:
    description = ''
    try:
        root = ET.fromstring(error.read().decode('utf-8', 'replace'))
        if root.tag == 'error':
            description = root.attrib.get('description') or root.attrib.get('code') or ''
    # no body, or not Jackett's error XML (a proxy's page, say): the status code has to do
    except (ET.ParseError, OSError):
        description = ''
    # the description of a failed site is a .NET stack trace; its first line is the message
    first_line = description.strip().splitlines()[0] if description.strip() else ''
    first_line = first_line.replace('Jackett.Common.IndexerException: ', '')[:160]
    return f"HTTP {error.code}" + (f": {first_line}" if first_line else '')


# the proxy qBittorrent passed in, if any
_HTTP_PROXY = os.environ.get('http_proxy', '')
_HTTPS_PROXY = os.environ.get('https_proxy', '')


# Turns the HTTP proxy qBittorrent passed in on or off. Off while talking to Jackett, which
# is on this machine or the local network; on only for a download link on some other host,
# as the official plugin does it (download_torrent). A SOCKS proxy cannot be toggled:
# qBittorrent's helpers apply it to the whole process at import and (5.1) offer no switch,
# so it stays whatever qBittorrent set, for the official plugin and this one alike.
def _set_proxy(enabled: bool) -> None:
    if enabled:
        os.environ['http_proxy'], os.environ['https_proxy'] = _HTTP_PROXY, _HTTPS_PROXY
    else:
        os.environ.pop('http_proxy', None)
        os.environ.pop('https_proxy', None)
    try:
        helpers.enable_socks_proxy(enabled)
    # no such switch in this qBittorrent: nothing to toggle
    except AttributeError:
        pass


_set_proxy(False)


# What has happened to a row's hash so far. A row that arrives with a hash is HASHED at
# once. A hash-less row starts NEW and moves right as the search goes:
#   NEW      not a lookup candidate (yet); shown as it is
#   QUEUED   waiting for a lookup slot, as a likely duplicate (row['lookup_tier'] 1) or in
#            the second round for the remaining rows (tier 2)
#   PENDING  lookup running
#   HASHED   has a hash, from the search reply or from a lookup
#   FAILED   lookup failed; shown as it is
#   SKIPPED  never looked up: private site, or lookups off
_NEW = 'new'
_QUEUED = 'queued'
_PENDING = 'pending'
_HASHED = 'hashed'
_FAILED = 'failed'
_SKIPPED = 'skipped'


class jackettdedup:
    # Read by qBittorrent from the class, before any instance exists. The url is the
    # plugin's identity: qBittorrent matches every result row's engine_url against the url
    # of each installed plugin, first match wins, enabled or not, and routes the row's
    # .torrent downloads to that plugin. The official plugin's url is the Jackett address
    # without a trailing slash, so this one keeps the slash: the two strings never match.
    # Requests go to api_url, the address proper.
    name = 'Jackett (dedup)'
    api_url = _setting('url').rstrip('/')
    url = api_url + '/'
    # qBittorrent's category names -> Jackett's category ids
    supported_categories = {
        'all': None,
        'anime': ['5070'],
        'books': ['8000'],
        'games': ['1000', '4000'],
        'movies': ['2000'],
        'music': ['3000'],
        'software': ['4000'],
        'tv': ['5000'],
    }
    # the connection settings, as the official plugin keeps them
    api_key = _setting('api_key')
    thread_count = _setting('concurrent_searches')

    # qBittorrent makes one instance per search. The state below is shared by the main
    # loop, the search threads and the lookup threads, so _lock guards all of it.
    def __init__(self) -> None:
        # every setting, as cfg.show_results_after and so on
        self.cfg = SimpleNamespace(**{key: _setting(key) for key in _DEFAULTS})
        # the link policy is one of four words; anything else is read as the default
        self.cfg.use_private_or_public_tracker = str(self.cfg.use_private_or_public_tracker).strip().lower()
        if self.cfg.use_private_or_public_tracker not in ('both', 'private', 'public', 'highest'):
            self.cfg.use_private_or_public_tracker = _DEFAULTS['use_private_or_public_tracker']
        self._strip_patterns = []
        for pattern in self.cfg.fold_strip_patterns:
            try:
                self._strip_patterns.append(re.compile(str(pattern), re.IGNORECASE))
            # a pattern that is not a valid regex is left out; the others still apply
            except re.error:
                pass
        self._lock = Lock()
        # every row from every site, in arrival order
        self._rows: List[Dict[str, Any]] = []
        # bumped when rows are added or change size; _start_lookups re-checks when it differs
        # from _candidates_version, the value at its last check
        self._rows_version = 0
        self._candidates_version = -1
        # sites whose rows are never looked up or folded
        self._private_indexer_ids: set = set()
        # one lookup per page: (site id, details url) -> {'rows' waiting, 'done', 'found'}
        self._lookups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # site id -> lookups running on it, for lookup_per_indexer
        self._in_flight: Dict[str, int] = {}
        # rows with a size, sorted by size, and their sizes for bisect; the rows without
        # a size apart. All for _looks_like_duplicate.
        self._sized_rows: List[Dict[str, Any]] = []
        self._sizes: List[int] = []
        self._unsized_rows: List[Dict[str, Any]] = []
        # hashes on screen, or hidden behind a shown row
        self._shown_hashes: set = set()
        # shown rows later rows may fold into: {'words', 'size', 'seeds'}
        self._shown_primaries: List[Dict[str, Any]] = []
        # when the search started; the search threads check it before a retry
        self._started = time.monotonic()
        # whether the first rows are on screen; ends the second lookup round
        self._first_shown = False
        # for the log: site id -> search attempts made; site id -> why its search failed
        self._attempts: Dict[str, int] = {}
        self._site_failures: Dict[str, str] = {}
        self._folded_count = 0

    # ------------------------------------------------------------ qBittorrent API

    # Runs one search. Called by qBittorrent once per search; prints result rows to stdout
    # as they are ready and returns when the search is over.
    #   what: the query, URL-encoded
    #   cat: a key of supported_categories
    def search(self, what: str, cat: str = 'all') -> None:
        query = unquote(what)
        category = self.supported_categories[cat.lower()]
        # a broken settings file or an unset API key: said as a row, like the official plugin
        if _SETTINGS_ERROR:
            self._show_error(_SETTINGS_ERROR, query)
            return
        cfg = self.cfg
        # the deadlines count from here, so getting the site list is included
        started = time.monotonic()
        self._started = started
        indexer_ids = self._configured_indexers(query)
        # _configured_indexers has shown an error row
        if not indexer_ids:
            return

        # one search thread per site, or one combined request to Jackett when
        # concurrent_searches is 1
        if self.thread_count > 1:
            tasks = [(query, category, indexer_id) for indexer_id in indexer_ids]
        else:
            tasks = [(query, category, 'all')]
        search_pool = Pool(max(1, min(len(tasks), self.thread_count)))
        searches = search_pool.map_async(self._search_indexer, tasks)
        search_pool.close()
        # a second pool for the hash lookups: enough threads for every public site to run
        # its lookup_per_indexer at once, up to 64
        lookup_pool = None
        if cfg.lookup_hashes:
            public_count = sum(1 for indexer_id in indexer_ids if indexer_id not in self._private_indexer_ids)
            lookup_pool = Pool(min(64, max(1, cfg.lookup_per_indexer * public_count)))

        # The main loop, every 250 ms: start lookups for the rows that arrived since the
        # last tick, then show what is ready. Nothing is shown before show_results_after
        # unless every site has answered and every lookup is done. A likely duplicate whose
        # lookup is running stays hidden until it finishes; a second-round row is shown as
        # it is once the first showing has happened. The loop ends when everything is done
        # or at max_search_time.
        first_shown_at: Optional[float] = None
        while True:
            elapsed = time.monotonic() - started
            # the second lookup round ends when it is time for the first showing
            if first_shown_at is not None or elapsed >= cfg.show_results_after:
                self._first_shown = True
            # whether the sites have all answered is read before the lookups are sorted:
            # read after, rows that arrived in between would count as done without a lookup
            searches_done = searches.ready()
            if lookup_pool is not None:
                self._start_lookups(lookup_pool)
            done = searches_done and not self._lookups_pending()
            # the first showing, once it is time; every tick after that shows the late rows
            if first_shown_at is not None or done or elapsed >= cfg.show_results_after:
                self._first_shown = True
                self._show_ready_rows()
                first_shown_at = elapsed if first_shown_at is None else first_shown_at
            if done or elapsed >= cfg.max_search_time:
                break
            time.sleep(0.25)

        # the search is over: rows still waiting for a lookup are shown as they are, and
        # lookups still running are abandoned
        self._show_ready_rows(show_unresolved=True)
        if lookup_pool is not None:
            lookup_pool.terminate()
        if cfg.debug_log:
            self._append_log(query, time.monotonic() - started, first_shown_at)

    # -------------------------------------------------------------------- search

    # Gets the list of sites Jackett is set up with, and notes which are private or
    # semi-private: their rows are never looked up (a download counts against the account)
    # and never folded.
    #   returns: the site ids; [] after showing an error row when the list could not be had
    def _configured_indexers(self, query: str) -> List[str]:
        params = urlencode([('apikey', self.api_key), ('t', 'indexers'), ('configured', 'true')])
        text, why = self._get_text(f"{self.api_url}/api/v2.0/indexers/all/results/torznab/api?{params}")
        # Jackett is not running, the url is wrong, or Jackett refused (a wrong API key
        # comes back as "HTTP 400: Invalid API Key")
        if text is None:
            self._show_error(f"could not get the indexer list from Jackett ({why})", query)
            return []
        # Jackett answers <indexers>...</indexers>, or <error .../> for a wrong API key. A
        # reverse proxy in front of it may answer with its login page instead, which is
        # either not XML at all or XML whose top element is not <indexers>.
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            root = None
        if root is not None and root.tag == 'error':
            self._show_error(f"Jackett error: {root.attrib.get('description') or root.attrib.get('code') or 'unknown'}", query)
            return []
        if root is None or root.tag != 'indexers':
            self._show_error('the reply from the Jackett url is not an indexer list (wrong url, or a proxy login page?)', query)
            return []
        # for each <indexer>: take its id (an entry without one cannot be searched) and
        # note the private ones
        indexer_ids = []
        for indexer in root.findall('indexer'):
            indexer_id = indexer.attrib.get('id')
            if not indexer_id:
                continue
            indexer_ids.append(indexer_id)
            if _child_text(indexer, 'type').lower() in ('private', 'semi-private'):
                self._private_indexer_ids.add(indexer_id)
        if not indexer_ids:
            self._show_error('no indexers are configured in Jackett', query)
        return indexer_ids

    # Searches one site and adds its rows to _rows, trying again when the site fails. Runs
    # in a search thread. Never raises: a broken site shows an error row instead of taking
    # the whole search down.
    #   task: (query, category ids or None for all, site id or 'all' for one combined request)
    def _search_indexer(self, task: Tuple[str, Optional[List[str]], str]) -> None:
        query, category, indexer_id = task
        cfg = self.cfg
        params = [('apikey', self.api_key), ('q', query)]
        if category is not None:
            params.append(('cat', ','.join(category)))
        url = f"{self.api_url}/api/v2.0/indexers/{indexer_id}/results/torznab/api?{urlencode(params)}"
        # One attempt, then up to search_retries more when the site failed inside Jackett or
        # Jackett could not be reached: such failures often clear on the next try. No retry
        # for a rate limit (Jackett answers HTTP 429 and names a wait that is longer than
        # the search), and none when fewer than 5 seconds of the search are left.
        attempts = 1 + max(0, cfg.search_retries)
        for attempt in range(1, attempts + 1):
            text, why = self._get_text(url)
            if text is not None:
                break
            seconds_left = cfg.max_search_time - (time.monotonic() - self._started)
            if attempt == attempts or why.startswith('HTTP 429') or seconds_left < 5:
                with self._lock:
                    self._attempts[indexer_id] = attempt
                    self._site_failures[indexer_id] = why
                self._show_error(f"indexer '{indexer_id}' failed after {attempt} attempt{'s' if attempt > 1 else ''}: {why}", query)
                return
            time.sleep(2)
        with self._lock:
            self._attempts[indexer_id] = attempt
        try:
            # the reply is an RSS feed: <rss><channel><item>...</item>...
            channel = ET.fromstring(text).find('channel')
            items = channel.findall('item') if channel is not None else []
            rows = [row for row in (self._row_from_item(item, indexer_id) for item in items) if row is not None]
        # anything unexpected in the reply (not XML, a field that is not a number...): one
        # error row for this site. A thread that raised would be lost silently.
        except Exception as error:
            self._show_error(f"indexer '{indexer_id}' returned something unreadable ({type(error).__name__})", query)
            return
        with self._lock:
            for row in rows:
                row['id'] = len(self._rows)
                self._rows.append(row)
            # tells _start_lookups there are new rows to check
            if rows:
                self._rows_version += 1

    # Reads one result in Jackett's reply into the row dict the rest of the file works on.
    #   item: the <item> element
    #   searched_indexer_id: the site searched; 'all' for a combined request, in which case
    #                        the item names its own site
    #   returns: the row, or None when the item has no title or neither a magnet nor a download link
    def _row_from_item(self, item: Any, searched_indexer_id: str) -> Optional[Dict[str, Any]]:
        # Runs of whitespace become one space. Some sites put a line break inside a title,
        # and qBittorrent reads one result per line: the row would arrive as two broken
        # rows, the second named after the size.
        title = ' '.join(_child_text(item, 'title').split())
        if not title:
            return None
        indexer_element = item.find('jackettindexer')
        indexer_id = (indexer_element.attrib.get('id') if indexer_element is not None else None) or searched_indexer_id
        # Where the hash comes from: Jackett gives it as its own attribute, or inside the
        # magnet attribute, or not at all, in which case the site only gave a download link
        # and the hash has to be looked up. Sites without .torrent files put the magnet in
        # <link>, where the download link would otherwise be.
        magnet = _torznab_attr(item, 'magneturl')
        link = _child_text(item, 'link')
        if link.lower().startswith('magnet:'):
            magnet, link = magnet or link, ''
        # nothing to download: useless as a result
        if not magnet and not link:
            return None
        infohash = _normalize_hash(_torznab_attr(item, 'infohash')) or _hash_from_magnet(magnet) or ''
        seeds = int(_torznab_attr(item, 'seeders') or -1)
        peers = int(_torznab_attr(item, 'peers') or -1)
        # no <size>, or not a number: -1
        try:
            size = int(_child_text(item, 'size'))
        except ValueError:
            size = -1
        # Jackett writes dates like "Sat, 14 Mar 2015 17:10:42 -0400"
        try:
            pub_date = int(datetime.datetime.strptime(_child_text(item, 'pubDate'), '%a, %d %b %Y %H:%M:%S %z').timestamp())
        except ValueError:
            pub_date = -1
        private = indexer_id in self._private_indexer_ids
        # the row's starting lookup state, see the states above the class
        if infohash:
            state = _HASHED
        elif link and not private and self.cfg.lookup_hashes:
            state = _NEW
        else:
            state = _SKIPPED
        return {
            # position in _rows, set when appended
            'id': -1,
            'title': title,
            'words': _title_words(title, self._strip_patterns),
            # as shown in the row's suffix
            'indexer_name': _child_text(item, 'jackettindexer') or indexer_id,
            'indexer_id': indexer_id,
            'private': private,
            'magnet': magnet,
            # from a looked-up .torrent
            'trackers': [],
            # Jackett download link, '' when magnet-only
            'link': link,
            'infohash': infohash,
            # bytes, -1 unknown
            'size': size,
            'seeds': seeds,
            'leech': peers - seeds if seeds >= 0 and peers >= 0 else -1,
            # the lookup key
            'details_url': _child_text(item, 'comments') or _child_text(item, 'guid'),
            'pub_date': pub_date,
            'state': state,
            # which lookup round the row is in: 1 = likely duplicate, 2 = remaining rows,
            # 0 = not a candidate
            'lookup_tier': 0,
            # search | magnet | torrent | shared | failed | '', for the log
            'hash_from': 'search' if infohash else '',
            'shown': False,
        }

    # ------------------------------------------------------------- hash lookups

    # Tells search whether any lookup is still worth waiting for: one queued or running
    # for a row not on screen yet. A second-round row shown unresolved at the first
    # showing does not keep the search alive.
    def _lookups_pending(self) -> bool:
        with self._lock:
            return any(row['state'] in (_QUEUED, _PENDING) and not row['shown'] for row in self._rows)

    # Starts hash lookups. Called every tick by search. Whenever rows arrived since the last
    # check, hash-less rows are sorted into the two rounds: likely duplicates (tier 1,
    # _looks_like_duplicate) and, until the first showing, the rest (tier 2). The queued
    # rows are then started, tier 1 first, as far as the lookup limits allow.
    #   pool: the lookup thread pool
    def _start_lookups(self, pool: Any) -> None:
        cfg = self.cfg
        second_round_open = cfg.lookup_remaining_hashes and not self._first_shown
        to_submit: List[Tuple[Tuple[str, str], str]] = []
        with self._lock:
            # Rows arrived since the last check: compare every NEW row against all rows, old
            # and new, because a row arriving late can be the twin of an early one. A
            # second-round row is compared again, whether it is still waiting or its lookup
            # is already running, since a twin arriving now moves it to the first round: it
            # is then held until its hash is known instead of shown as it is at the first
            # showing. A row already on screen is left alone: it cannot change any more.
            if self._rows_version != self._candidates_version:
                self._sort_rows_by_size()
                for row in self._rows:
                    if row['shown'] or row['lookup_tier'] == 1 or row['state'] not in (_NEW, _QUEUED, _PENDING):
                        continue
                    if self._looks_like_duplicate(row):
                        # a lookup already running keeps running; only the round changes
                        if row['state'] == _NEW:
                            row['state'] = _QUEUED
                        row['lookup_tier'] = 1
                    elif row['state'] == _NEW and second_round_open:
                        row['state'], row['lookup_tier'] = _QUEUED, 2
                self._candidates_version = self._rows_version

            # The order lookups start in: likely duplicates first, best-seeded first among
            # them; then the second round, rows without a seed count first (a merge gives
            # them one), then by seeds.
            def start_order(row: Dict[str, Any]) -> Tuple[int, int, int]:
                return (row['lookup_tier'], 0 if row['lookup_tier'] == 2 and row['seeds'] < 0 else 1, -row['seeds'])

            # For each queued row not yet on screen: share the lookup when its page is
            # already being looked up for another row; otherwise start one, unless the
            # row's site has its share of lookups running already, in which case the row
            # waits for a later tick. A second-round row may use all but one of the site's
            # lookup_per_indexer slots, so a likely duplicate arriving later never waits
            # behind a full set of them; and none starts once the first rows are shown.
            queued = sorted((row for row in self._rows if row['state'] == _QUEUED and not row['shown']), key=start_order)
            for row in queued:
                if row['lookup_tier'] == 2 and not second_round_open:
                    continue
                key = (row['indexer_id'], row['details_url'] or row['link'])
                lookup = self._lookups.get(key)
                if lookup is not None:
                    lookup['rows'].append(row)
                    row['state'] = _PENDING
                    if lookup['done']:
                        self._record_hash(row, lookup['found'], shared=True)
                    continue
                slots = cfg.lookup_per_indexer if row['lookup_tier'] == 1 else cfg.lookup_per_indexer - 1
                if self._in_flight.get(row['indexer_id'], 0) >= max(1, slots):
                    continue
                self._in_flight[row['indexer_id']] = self._in_flight.get(row['indexer_id'], 0) + 1
                self._lookups[key] = {'rows': [row], 'done': False, 'found': None, 'tier': row['lookup_tier']}
                row['state'] = _PENDING
                to_submit.append((key, row['link']))
        # submitted outside the lock: a lookup that finishes at once would call
        # _finish_lookup, which takes the lock itself
        for key, link in to_submit:
            pool.apply_async(self._lookup_hash, (key, link), callback=self._finish_lookup,
                             error_callback=lambda _error, key=key: self._finish_lookup((key, self._nothing_found())))

    # Rebuilds the size-sorted row lists that _looks_like_duplicate searches with bisect,
    # so each check costs a slice instead of a pass over every row. Lock must be held.
    def _sort_rows_by_size(self) -> None:
        self._sized_rows = sorted((row for row in self._rows if row['size'] > 0), key=lambda row: row['size'])
        self._sizes = [row['size'] for row in self._sized_rows]
        self._unsized_rows = [row for row in self._rows if row['size'] <= 0]

    # Decides whether a hash-less row resembles a row from another site closely enough that
    # a lookup is worth a page fetch: a similar size (size_tolerance) and enough shared
    # title words (lookup_title_similarity); the title alone when either row reports no
    # size. Only decides that; merging is still by hash. Lock must be held.
    def _looks_like_duplicate(self, row: Dict[str, Any]) -> bool:
        cfg = self.cfg
        abs_tolerance = int(cfg.size_tolerance_mb * 1024 * 1024)
        if row['size'] > 0:
            # the rows of a similar size are one slice of the size-sorted list, cut a little
            # wider than the tolerance (which is measured against the larger size);
            # _sizes_match then decides exactly. For each row in the slice: from another
            # site, size matches and enough shared words = this row is a candidate.
            delta = int(max(cfg.size_tolerance * row['size'] * 1.1, abs_tolerance))
            start = bisect.bisect_left(self._sizes, row['size'] - delta)
            stop = bisect.bisect_right(self._sizes, row['size'] + delta)
            for other in self._sized_rows[start:stop]:
                if other['indexer_id'] != row['indexer_id'] \
                        and _sizes_match(row['size'], other['size'], cfg.size_tolerance, abs_tolerance) \
                        and _word_overlap(row['words'], other['words']) >= cfg.lookup_title_similarity:
                    return True
            # no sized twin; the rows without a size remain to be checked, by title
            others = self._unsized_rows
        else:
            others = self._rows
        # a size is missing on one side or the other: the title has to carry the decision
        return any(other['indexer_id'] != row['indexer_id']
                   and _word_overlap(row['words'], other['words']) >= cfg.lookup_title_similarity
                   for other in others)

    # The result of a lookup that found nothing, in the shape _lookup_hash returns.
    @staticmethod
    def _nothing_found() -> Dict[str, Any]:
        return {'infohash': None, 'magnet': None, 'trackers': [], 'size': 0, 'how': 'failed'}

    # Does one lookup: fetches the row's download link from Jackett and reads the hash out
    # of what comes back, a magnet or a .torrent. Runs in a lookup thread.
    #   key: (site id, details url) the result belongs to
    #   returns: (key, found) with found = {'infohash' or None, 'magnet' or None,
    #            'trackers' and 'size' read from a .torrent, 'how': magnet | torrent | failed}
    def _lookup_hash(self, key: Tuple[str, str], link: str) -> Tuple[Tuple[str, str], Dict[str, Any]]:
        found = self._nothing_found()
        kind, payload = self._fetch_download_link(link)
        if kind == 'magnet':
            found.update(infohash=_hash_from_magnet(payload), magnet=payload, how='magnet')
        elif kind == 'torrent':
            torrent = _torrent_dict(payload)
            found.update(infohash=_hash_from_torrent(payload), how='torrent')
            if torrent is not None:
                found.update(trackers=_trackers_from_torrent(torrent), size=_size_from_torrent(torrent))
        # else: Jackett answered with an error page, or not at all; found says 'failed'
        return (key, found)

    # Stores a finished lookup's result on every row waiting for that page and frees the
    # site's in-flight slot. Called by the pool when _lookup_hash returns, or with a
    # 'failed' result when it raised.
    def _finish_lookup(self, result: Tuple[Tuple[str, str], Dict[str, Any]]) -> None:
        key, found = result
        with self._lock:
            lookup = self._lookups.get(key)
            # a guard: not a lookup of this search, or finished already
            if lookup is None or lookup['done']:
                return
            lookup['done'] = True
            lookup['found'] = found
            indexer_id = key[0]
            self._in_flight[indexer_id] = max(0, self._in_flight.get(indexer_id, 0) - 1)
            # every row waiting for this page gets the result; the first one is the lookup's
            # own, the others shared it (a guard on the state: all of them are pending)
            for position, row in enumerate(lookup['rows']):
                if row['state'] == _PENDING:
                    self._record_hash(row, found, shared=position > 0)

    # Applies a lookup result to one row: its hash, the magnet, trackers or size that came
    # with it, and its new state. Lock must be held.
    #   found: from _lookup_hash
    #   shared: another row's lookup supplied it (noted in the log)
    def _record_hash(self, row: Dict[str, Any], found: Dict[str, Any], shared: bool) -> None:
        # the fetch failed or returned something that is not a torrent: the row stays hash-less
        if not found['infohash']:
            row['state'], row['hash_from'] = _FAILED, 'failed'
            return
        row['infohash'] = found['infohash']
        row['state'] = _HASHED
        row['hash_from'] = 'shared' if shared else found['how']
        if found['magnet']:
            row['magnet'] = found['magnet']
        row['trackers'] = list(found['trackers'])
        # a row whose site reported no size takes the .torrent's; the size-sorted lists
        # are rebuilt at the next check so later rows can match it by size
        if row['size'] <= 0 and found['size'] > 0:
            row['size'] = found['size']
            self._rows_version += 1
        # a second-round row went on screen before its lookup finished (the round ended at
        # the first showing); its hash is on screen now, so a twin arriving later is dropped
        if row['shown']:
            self._shown_hashes.add(found['infohash'])

    # ----------------------------------------------------------------- showing

    # Shows every row that is ready and not on screen yet. Called every tick from the first
    # showing on, and once more when the search ends.
    #   show_unresolved: also show the likely duplicates whose lookup has not finished;
    #                    only at the end of the search, when there is nothing more to wait for
    def _show_ready_rows(self, show_unresolved: bool = False) -> None:
        with self._lock:
            # For each row: skip it when it is on screen already; a row with a hash joins
            # the group of that hash; a likely duplicate whose lookup is queued or running
            # is left for a later tick (unless show_unresolved); any other hash-less row,
            # a second-round row with its lookup still running included, is a group of its
            # own.
            groups: Dict[Any, List[Dict[str, Any]]] = {}
            for row in self._rows:
                if row['shown']:
                    continue
                if row['infohash']:
                    key: Any = row['infohash']
                elif row['state'] in (_QUEUED, _PENDING) and row['lookup_tier'] == 1 and not show_unresolved:
                    continue
                else:
                    key = ('row', row['id'])
                groups.setdefault(key, []).append(row)
            # For each group: mark its rows shown and its hash on screen, then build the one
            # result for it. A group whose hash is on screen already is a late copy (a slow
            # site, a late lookup) and is dropped, since the row on screen cannot be changed.
            batch = []
            for key, rows in groups.items():
                already_shown = key in self._shown_hashes
                for row in rows:
                    row['shown'] = True
                if isinstance(key, str):
                    self._shown_hashes.add(key)
                if not already_shown:
                    batch.append(self._merged_row(rows))
            if self.cfg.fold_lookalikes:
                batch = self._fold_lookalikes(batch)
        # printed outside the lock: printing needs no shared state
        for entry in batch:
            self._show_result(entry['result'])

    # Builds the one result row for a group of rows with the same hash. The best-seeded row
    # supplies the title, size and details link; the seed count is the highest any site
    # reported; the suffix names every site. Lock must be held.
    #   rows: the group, any order; a lone hash-less row is a group of one
    #   returns: {'result': the dict prettyPrinter takes, 'seeds', 'size', 'private', 'words'};
    #            the extras are what _fold_lookalikes compares
    def _merged_row(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        cfg = self.cfg
        rows = sorted(rows, key=lambda row: row['seeds'], reverse=True)
        best = rows[0]
        link = self._merged_link(best['infohash'], rows)
        # the best copy's size, or any copy's when the best reports none
        size = best['size'] if best['size'] > 0 else next((row['size'] for row in rows if row['size'] > 0), -1)
        pub_dates = [row['pub_date'] for row in rows if row['pub_date'] != -1]
        # the site names in seed order, each once: two rows from one site count once
        indexer_names: List[str] = []
        for row in rows:
            if row['indexer_name'] not in indexer_names:
                indexer_names.append(row['indexer_name'])
        name = best['title']
        # "[TPB, 1337x +2]": the first max_indexers_shown names, then how many more
        if cfg.show_indexers:
            names_shown = max(1, cfg.max_indexers_shown)
            tag = ', '.join(indexer_names[:names_shown]) + (f" +{len(indexer_names) - names_shown}" if len(indexer_names) > names_shown else '')
            name = f"[{tag}] {name}" if cfg.indexers_before_title else f"{name} [{tag}]"
        result = {
            'name': name,
            'link': link,
            'size': f"{size} B" if size > 0 else -1,
            'seeds': max(row['seeds'] for row in rows),
            'leech': max(row['leech'] for row in rows),
            'desc_link': next((row['details_url'] for row in rows if row['details_url']), ''),
            # qBittorrent finds the plugin to download with by this; it must equal the class url
            'engine_url': self.url,
            # the earliest upload
            'pub_date': min(pub_dates) if pub_dates else -1,
        }
        # private when any copy is: such a row is never folded and never hides another, even
        # when its best-seeded copy is public
        return {'result': result, 'seeds': best['seeds'], 'size': size, 'private': any(row['private'] for row in rows), 'words': best['words']}

    # Picks the one link for a group. The public copies pool into one magnet
    # (_merged_magnet); a private copy's .torrent link carries the user's passkey and
    # counts toward their ratio, so a row shared by private and public sites follows
    # use_private_or_public_tracker (see the setting). Lock must be held.
    #   infohash: the group's key; '' for a hash-less row
    #   rows: the group, best-seeded first
    #   returns: a magnet, a Jackett download link, or '' when no copy has either
    def _merged_link(self, infohash: str, rows: List[Dict[str, Any]]) -> str:
        private_rows = [row for row in rows if row['private']]
        public_rows = [row for row in rows if not row['private']]
        # the private side's link: the best-seeded private copy's .torrent, or its magnet
        # when the site gave no .torrent
        private_link = next((row['link'] or row['magnet'] for row in private_rows if row['link'] or row['magnet']), '')
        # the public side's: one magnet pooling the public copies, else the best public .torrent
        public_link = self._merged_magnet(infohash, public_rows) or next((row['link'] for row in public_rows if row['link']), '')
        if not private_rows:
            return public_link
        if not public_rows:
            return private_link
        policy = self.cfg.use_private_or_public_tracker
        if policy == 'private':
            return private_link or public_link
        if policy == 'public':
            return public_link or private_link
        if policy == 'highest':
            return (private_link or public_link) if rows[0]['private'] else (public_link or private_link)
        # both: every copy's trackers in one magnet when a private site gave a magnet; else
        # the private .torrent (see the setting's note on the private flag)
        if any(row['magnet'] for row in private_rows):
            return self._merged_magnet(infohash, rows) or private_link or public_link
        return private_link or public_link

    # Builds one magnet link for the given copies: the hash, the first display name, every
    # copy's trackers (from magnets and from looked-up .torrent files, deduped) and any
    # other magnet parameters, such as web seeds. More trackers = more peers found.
    #   infohash: the group's key, a v1 hash or "v2:" + a v2 hash; '' for a hash-less row
    #   rows: the copies to pool, best-seeded first
    #   returns: the magnet; '' when no copy has one (a lone .torrent-resolved row keeps
    #            its .torrent link); the first magnet unchanged when the hash is unknown
    def _merged_magnet(self, infohash: str, rows: List[Dict[str, Any]]) -> str:
        magnets = [row['magnet'] for row in rows if row['magnet']]
        if not magnets:
            return ''
        # no hash, no merged magnet: pass the one there is through
        if not infohash:
            return magnets[0]
        display_name, trackers, extras = '', [], []
        # the hash is written once, below, in its own form: urn:btih for a v1 key, urn:btmh
        # for a v2 key; that same part of each magnet is skipped in the extras loop, while
        # a hybrid magnet's other hash part is kept
        if infohash.startswith('v2:'):
            own_xt, own_prefix = f"urn:btmh:1220{infohash[3:]}", 'urn:btmh:'
        else:
            own_xt, own_prefix = f"urn:btih:{infohash}", 'urn:btih:'
        # the trackers read from looked-up .torrent files first
        for row in rows:
            trackers += [tracker for tracker in row['trackers'] if tracker not in trackers]
        # then, for each magnet: its display name (the first one wins), its trackers, and
        # every other parameter except the hash itself, which is written once below
        for magnet in magnets:
            params = _magnet_params(magnet)
            display_name = display_name or (params.get('dn') or [''])[0]
            trackers += [tracker.strip() for tracker in params.get('tr', []) if tracker.strip() and tracker.strip() not in trackers]
            for param, values in params.items():
                if param in ('dn', 'tr'):
                    continue
                for value in values:
                    if param == 'xt' and value.lower().startswith(own_prefix):
                        continue
                    if (param, value) not in extras:
                        extras.append((param, value))
        parts = [f"magnet:?xt={own_xt}", f"&dn={quote(display_name or rows[0]['title'], safe='')}"]
        parts += [f"&tr={quote(tracker, safe='')}" for tracker in trackers]
        parts += [f"&{quote(param, safe='')}={quote(value, safe=':')}" for param, value in extras]
        return ''.join(parts)

    # Hides results that are the same files in another torrent: identical cleaned words,
    # same size, different hash. The best-seeded one stays and gets "+N alt"; the torrents
    # are not combined. Lock must be held.
    #   batch: entries from _merged_row
    #   returns: the entries to show, best-seeded first
    def _fold_lookalikes(self, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cfg = self.cfg
        abs_tolerance = int(cfg.size_tolerance_mb * 1024 * 1024)

        # Two results are lookalikes when both have a size and words, the sizes match and
        # the words are identical, in order.
        def is_lookalike(entry: Dict[str, Any], other: Dict[str, Any]) -> bool:
            if entry['size'] <= 0 or other['size'] <= 0 or not entry['words'] or not other['words']:
                return False
            if not _sizes_match(entry['size'], other['size'], cfg.size_tolerance, abs_tolerance):
                return False
            return entry['words'] == other['words']

        # For each result, best-seeded first so the first of each lookalike group is the one
        # kept: a private result is always kept and never swallows others; a lookalike of a
        # result shown in an earlier batch is hidden, unless it has more seeds (the earlier
        # row cannot get a "+1 alt", so the better row is shown instead); a lookalike of a
        # result kept in this batch folds into it; anything else is kept.
        kept: List[Dict[str, Any]] = []
        for entry in sorted(batch, key=lambda entry: entry['seeds'], reverse=True):
            entry['folded'] = 0
            if entry['private']:
                kept.append(entry)
                continue
            earlier = next((primary for primary in self._shown_primaries if is_lookalike(entry, primary)), None)
            if earlier is not None and entry['seeds'] <= earlier['seeds']:
                self._folded_count += 1
                continue
            primary = next((candidate for candidate in kept if not candidate['private'] and is_lookalike(entry, candidate)), None)
            if primary is not None:
                primary['folded'] += 1
                self._folded_count += 1
                continue
            kept.append(entry)
        # the kept results are remembered for later batches only now: done inside the loop,
        # the earlier-batch check would catch this batch's own entries and lose the count
        for entry in kept:
            if entry['folded']:
                entry['result']['name'] += f" +{entry['folded']} alt"
            if not entry['private']:
                self._shown_primaries.append({'words': entry['words'], 'size': entry['size'], 'seeds': entry['seeds']})
        return kept

    # Prints one result for qBittorrent, as one line of '|'-separated fields. A line break
    # inside a field would split the row in two, so none may remain. A '|' inside the name
    # is qBittorrent's own business (its printer replaces or encodes it; escaping it here
    # too would show up as a literal %7C); in the other fields it is escaped.
    def _show_result(self, result: Dict[str, Any]) -> None:
        safe = {}
        for key, value in result.items():
            if isinstance(value, str):
                value = value.replace('\r', ' ').replace('\n', ' ')
                if key != 'name':
                    value = value.replace('|', '%7C')
            safe[key] = value
        with _PRINT_LOCK:
            prettyPrinter(safe)

    # Prints an error as a result row, where the user will see it, as the official plugin
    # does. The row's description link opens the official plugin's help page.
    def _show_error(self, message: str, query: str) -> None:
        self._show_result({
            'link': self.url,
            'name': f"Jackett: {message}! Right-click this row and select 'Open description page' to open help. Configuration file: '{_SETTINGS_PATH}' Search: '{query}'",
            'size': -1,
            'seeds': -1,
            'leech': -1,
            'engine_url': self.url,
            'desc_link': 'https://github.com/qbittorrent/search-plugins/wiki/How-to-configure-Jackett-plugin',
            'pub_date': -1,
        })

    # Appends this search to jackettdedup.log: a totals line, a line per site (a "!" line
    # for a site whose search failed), then one "?" line per public row that was a lookup
    # candidate but ended without a hash, with the reason. Never raises: the log must not
    # break a search.
    #   total: seconds the search took
    #   first_shown_at: seconds until the first rows were shown; None when nothing was shown
    def _append_log(self, query: str, total: float, first_shown_at: Optional[float]) -> None:
        try:
            with self._lock:
                rows = self._rows
                shared = sum(len(lookup['rows']) - 1 for lookup in self._lookups.values())
                shown = len(self._shown_hashes) + sum(1 for row in rows if row['shown'] and not row['infohash'])
                retried = sum(1 for attempts in self._attempts.values() if attempts > 1)
                # the second round's score: lookups it started, and rows it gave a hash that
                # another row shares (merged, or dropped as a copy already on screen)
                second_round = sum(1 for lookup in self._lookups.values() if lookup['tier'] == 2)
                hashes_seen: Dict[str, int] = {}
                for row in rows:
                    if row['infohash']:
                        hashes_seen[row['infohash']] = hashes_seen.get(row['infohash'], 0) + 1
                second_round_matched = sum(1 for row in rows if row['lookup_tier'] == 2 and row['infohash'] and hashes_seen[row['infohash']] > 1)
                lines = [f"{datetime.datetime.now().isoformat(timespec='seconds')} search={query!r} rows={len(rows)} "
                         f"shown={shown} lookups={len(self._lookups)} shared={shared} folded={self._folded_count} "
                         f"second_round_lookups={second_round} second_round_matched={second_round_matched} "
                         f"sites_retried={retried} sites_failed={len(self._site_failures)} "
                         f"first_shown={first_shown_at if first_shown_at is not None else -1:.1f}s total={total:.1f}s"]
                # one line per site: how many rows it gave, where their hashes came from, and
                # how many attempts its search took
                for indexer_id in sorted({row['indexer_id'] for row in rows}):
                    site_rows = [row for row in rows if row['indexer_id'] == indexer_id]
                    states: Dict[str, int] = {}
                    for row in site_rows:
                        states[row['state']] = states.get(row['state'], 0) + 1
                    lines.append(f"    {indexer_id:<24} private={str(indexer_id in self._private_indexer_ids):<5} rows={len(site_rows):<4} "
                                 f"hash_at_search={sum(1 for row in site_rows if row['hash_from'] == 'search'):<4} "
                                 f"looked_up={sum(1 for row in site_rows if row['hash_from'] in ('magnet', 'torrent', 'shared')):<4} "
                                 f"failed={sum(1 for row in site_rows if row['hash_from'] == 'failed'):<3} "
                                 f"attempts={self._attempts.get(indexer_id, 1)} states={states}")
                # one "!" line per site whose search failed every attempt
                for indexer_id in sorted(self._site_failures):
                    lines.append(f"    ! {indexer_id:<22} search failed after {self._attempts.get(indexer_id, 1)} attempt(s): {self._site_failures[indexer_id]}")
                # one "?" line per candidate row that ended without a hash, the 60 best-seeded
                # at most to keep the log readable; the reason depends on the row's round
                reasons = {(1, _QUEUED): 'never got a lookup slot before max_search_time',
                           (1, _PENDING): 'lookup still running at max_search_time',
                           (2, _QUEUED): 'second round: no lookup slot before the first showing',
                           (2, _PENDING): 'second round: lookup still running at the first showing',
                           (1, _FAILED): 'lookup failed (Jackett error, or not a torrent or magnet)',
                           (2, _FAILED): 'second round: lookup failed (Jackett error, or not a torrent or magnet)'}
                unresolved = [row for row in rows if (row['lookup_tier'], row['state']) in reasons]
                for row in sorted(unresolved, key=lambda row: row['seeds'], reverse=True)[:60]:
                    lines.append(f"    ? {row['indexer_id']:<16} seeds={row['seeds']:<5} size={row['size']:<12} "
                                 f"{reasons[(row['lookup_tier'], row['state'])]}: {row['title'][:90]}")
            with open(_LOG_PATH, 'a', encoding='utf-8') as log_file:
                log_file.write('\n'.join(lines) + '\n')
        # a read-only plugin folder: the search ran; only the log is lost
        except OSError:
            pass

    # --------------------------------------------------------------------- HTTP

    # Fetches a URL with a browser-like User-Agent.
    #   raises: urllib errors. A redirect to a magnet raises HTTPError with .url = the
    #           magnet, because urllib refuses to follow a non-http scheme; the callers
    #           read the magnet out of that error.
    def _http_get(self, url: str) -> bytes:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        opener.addheaders = [('User-Agent', _user_agent())]
        return opener.open(url, timeout=float(self.cfg.request_timeout or 45)).read()

    # Fetches a URL as text, for Jackett's XML API.
    #   returns: (the body, '') on success; (None, why) on failure, with why = 'connection
    #            error' when Jackett could not be reached, or Jackett's own error message
    #            when it answered with one (see _jackett_error)
    def _get_text(self, url: str) -> Tuple[Optional[str], str]:
        try:
            return self._http_get(url).decode('utf-8'), ''
        # any HTTP error is a failure here: the XML endpoints never redirect to a magnet
        except urllib.error.HTTPError as error:
            return None, _jackett_error(error)
        # connection refused, timeout, bad certificate...: the caller shows an error row
        except Exception:
            return None, 'connection error'

    # Fetches a Jackett download link and says what came back. Jackett answers with a
    # redirect to a magnet for magnet-only sites and with the .torrent bytes otherwise; an
    # HTML page means Jackett failed on its side (the site is down, or behind Cloudflare).
    #   raise_errors: re-raise fetch errors so qBittorrent can report them (download_torrent)
    #                 instead of returning ('error', None) (lookups)
    #   returns: ('magnet', url) | ('torrent', bytes) | ('error', None)
    def _fetch_download_link(self, link: str, raise_errors: bool = False) -> Tuple[str, Any]:
        try:
            data = self._http_get(link)
        # a redirect to a magnet is the good case (see _http_get); any other HTTP error is a failure
        except urllib.error.HTTPError as error:
            target = str(getattr(error, 'url', '') or '')
            if error.code in (301, 302, 303, 307, 308) and target.lower().startswith('magnet:'):
                return ('magnet', target)
            if raise_errors:
                raise
            return ('error', None)
        except Exception:
            if raise_errors:
                raise
            return ('error', None)
        # gzip magic bytes: some setups compress the .torrent
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        # a magnet as the body instead of a redirect
        if data[:7].lower() == b'magnet:':
            return ('magnet', data.decode('utf-8', 'replace'))
        # a .torrent file starts with its dict
        if data[:1] == b'd':
            return ('torrent', data)
        # anything else: an HTML error page from Jackett
        return ('error', None)

    # Fetches a row's download link for qBittorrent when the user downloads the row. Prints
    # "<magnet> <link>" for a magnet, else saves the .torrent to a temp file and prints
    # "<path> <link>": the two forms qBittorrent accepts. The official plugin fetches the
    # link twice (once to look for a magnet, once to save); this fetches it once.
    #   raises: urllib errors when the fetch fails, so qBittorrent reports the failure
    def download_torrent(self, download_url: str) -> None:
        # a merged row's link: nothing to fetch
        if download_url.startswith('magnet:?'):
            print(download_url + " " + download_url)
            return
        # The proxy qBittorrent passed in is used only for a link on some other host, as the
        # official plugin does since 4.14. A Jackett link is on the Jackett host, this
        # machine or the local network, which a proxy elsewhere cannot reach: it goes
        # direct, like the searches.
        _set_proxy(urlsplit(download_url).netloc != urlsplit(self.api_url).netloc)
        try:
            kind, payload = self._fetch_download_link(download_url, raise_errors=True)
        finally:
            _set_proxy(False)
        if kind == 'magnet':
            print(payload + " " + download_url)
            return
        # Jackett answered with an error page: raise, so qBittorrent reports it instead of
        # saving the page as a torrent
        if kind == 'error':
            raise ValueError('Jackett did not return a torrent or a magnet')
        handle, path = tempfile.mkstemp()
        with os.fdopen(handle, 'wb') as torrent_file:
            torrent_file.write(payload)
        print(f"{path} {download_url}")
