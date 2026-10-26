# Changelog

The version is the `# VERSION:` line at the top of `jackettdedup.py`; qBittorrent reads it to offer updates, so it moves with every change to the plugin.

## 2.9 - 2026-10-25

- Second lookup round (`lookup_remaining_hashes`, default on): after the likely duplicates, every other hash-less public row is looked up too while the first results are still being waited for. Likely duplicates always go first and keep one lookup slot per site. Copies whose titles look nothing alike (a DHT site's file name against a release name) now merge, and a row from a site that reports no seeders takes the seed count of the copy that does. A second-round row whose lookup hasn't finished at `show_results_after` is shown as it is; the search doesn't wait for it, but its hash, once known, still drops later copies.
- `search_retries` default raised to 3.
- Debug log: `second_round_lookups`, `second_round_matched`, and second-round reasons on the `?` lines.

## 2.8 - 2026-10-25

- A site whose search fails is asked again before it is given up on (`search_retries`, default 1, 2 s apart). Cloudflare challenges and site hiccups usually clear on the second try. No retry for a rate limit (HTTP 429) or when the search is nearly out of time.
- Error rows carry Jackett's own message. A site failing inside Jackett comes back as HTTP 400 with an `<error>` body; the row now reads `indexer 'x' failed after 2 attempts: HTTP 400: Exception (x): …` instead of "connection error", which is kept for Jackett itself being unreachable. A wrong API key reads `HTTP 400: Invalid API Key`.
- Debug log: `sites_retried`, `sites_failed`, `attempts=` per site, and a `!` line for each site that failed every attempt.

## 2.7 - 2026-10-25

- Titles with a line break inside them no longer produce garbage rows. qBittorrent reads one result per line, so such a row arrived as two broken rows, the second named after the size (`536870912`). Whitespace in titles is collapsed before anything else happens. The official plugin has the same bug.
- A `|` in a title no longer shows as `%7C`. qBittorrent's printer handles `|` in names itself; escaping it in the plugin as well showed up as text. Also inherited from the official plugin.
- Download of a row whose Jackett link answers with an error page now raises, so qBittorrent reports the failure instead of saving the page as a `.torrent`.

## 2.6 - 2026-10-24

- Settings cut from 24 to 18. Removed: `hold_for_lookups` (rows with a running lookup now stay hidden until it finishes or `max_search_time`), `lookup_only_likely_duplicates`, `lookup_max`, `lookup_threads` (sized automatically: `lookup_per_indexer` × public sites, up to 64), `lookup_title_similarity_nosize`, `fold_title_similarity` (folding is identical cleaned words, full stop). Renamed `lookup_size_tolerance` / `_mb` to `size_tolerance` / `_mb`, since folding uses them too.
- A row whose site reports no size takes its size from the `.torrent` its lookup returns, and so becomes foldable.
- An existing `jackettdedup.json` is migrated: settings this version no longer has are dropped, new ones added with their default.
- A `.torrent` cut off mid-download is rejected instead of yielding a wrong hash.

## 2.5 - 2026-10-23

- Settings load as exists → read → create, with distinct messages for "cannot be read" and "not JSON".
- Numbers and booleans written as strings in the settings file (`"20"`, `"true"`) work.
- Comments and function headers rewritten throughout; every setting documented above its default.

## 2.4 - 2026-10-22

- Settings renamed to say what they do: `show_results_after`, `max_search_time`, `lookup_per_indexer`, `concurrent_searches`, `indexers_before_title`, … `max_trackers` removed; a merged magnet carries every tracker.
- Code map and plain-English problem statement in the file header.

## 2.0 - 2.3 - 2026-10-22

- Lookalike folding: rows with the same cleaned title and size but a different infohash (re-uploads) are hidden behind the best-seeded one, marked `+N alt`. Never merged: a merged row is always one real torrent.
- Titles compared as words in order and in any script, so `2.1` vs `1.2` and Cyrillic titles are told apart; site tags (`[TGx]`, `[YTS.MX]`, `www.site.com`) stripped first (`fold_strip_patterns`).
- Trackers read from looked-up `.torrent` files join the merged magnet alongside the magnets' own.
- `max_search_time` counts from the start of the search, indexer list included.
- A malformed indexer list, a Jackett `<error>`, or a proxy login page gives an error row instead of a crash or silence.
- `download_torrent` fetches the link once (the official plugin fetches twice).
- README, offline test suite (a local web server plays Jackett), knob documentation in the plugin file.

## 1.0 - 1.4 - 2026-10-21

- First version: rows with the same infohash merged into one, best seed count, every site's trackers in one magnet, `[Site, Site +N]` suffix; results shown after a fixed wait.
- Hash lookups for sites that report no infohash (1337x, LimeTorrents, …): the row's Jackett download link is fetched and the hash read from the magnet or `.torrent`. Public sites only, one fetch per details page per search, nothing kept between searches.
- Lookup candidates chosen by size (fraction plus an absolute allowance for display rounding) and cleaned-title word overlap; rows without a size qualify on title alone.
- Own settings file `jackettdedup.json`, seeded from the official plugin's `jackett.json`, so uninstalling the official plugin no longer deletes the settings.
- Fixed: the wait loop spun a CPU core after the searches finished; a magnet in `<link>` was treated as a download link.
