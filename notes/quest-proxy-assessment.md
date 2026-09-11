# QUEST-RL proxy assessment

Tested on 2026-09-11 from a Sandweave worker on Babel. The ten supplied proxies
authenticated and returned ten distinct exit addresses. They provide separate
agent exits, with site-dependent access. This test does not establish capacity
for thousands of simultaneous agents.

## Sample and requests

Six tasks were sampled without replacement from the 1,133-row
[QUEST-RL dataset](https://huggingface.co/datasets/osunlp/QUEST-RL-Data), using
Python `random.Random(20260911)`. The downloaded Parquet file and viewer rows
agree. The exact revision, file hash and seed are in
[the result data](quest-proxy-results.json).

| Row | Task | Representative sources |
| --- | --- | --- |
| 741 | Find a gaming laptop meeting detailed hardware requirements | ASUS specifications |
| 31 | Compare satellite SOS phones, coverage and service terms | Apple support |
| 918 | Analyze film-industry business models and financial evidence | Motion Picture Association, Netflix investor relations |
| 186 | Determine FCC wireline-outage reporting requirements | FCC, eCFR, an FCC PDF |
| 355 | Find HCI researchers, CHI papers and citation metrics | CMU faculty, ACM proceedings, Google Scholar |
| 1033 | Compare Oval Office and State of the Union addresses | House history, American Presidency Project |

For each task, the same query was sent to Google, Bing and DuckDuckGo. Twelve
source URLs were also requested. This produced **330 HTTP page requests**:
30 requests over each of ten proxies and one direct connection. Four concurrent
requests were allowed overall. The [request manifest](quest-proxy-requests.json)
records the queries and URLs. These are representative research steps, not
completed benchmark answers or scored agent trajectories.

The HTTP client used the same Chrome-style User-Agent and English-language
header for every route. It ignored inherited proxy environment variables.
Bodies, status codes, redirects, timing and challenge markers were saved locally.
Success below requires source content or actual search result entries; HTTP 200
alone was insufficient. Source content and sample search titles were inspected.

## Results

| Request type | Direct | Ten proxies combined |
| --- | ---: | ---: |
| Exit-address check | One Babel exit | 10 distinct exits; all authenticated |
| Bing searches with result entries | 6/6 | 60/60 |
| DuckDuckGo searches with result entries | 2/6 | 37/60 |
| Google searches with result entries in plain HTTP | 0/6 | 0/60 |
| Source URLs with usable content | 9/12 | 90/120 |

Google returned either a rate-limit/challenge page or a JavaScript shell.
The latter returned HTTP 200 but contained no search results. Bing result
titles were relevant to the submitted queries. DuckDuckGo alternated between
results and its image-selection challenge.

ASUS, Apple, MPA, eCFR, CMU, Google Scholar, House history, the American
Presidency Project and the FCC PDF supplied content on every route. The main
FCC page returned access denied. ACM returned a browser-verification challenge.
Netflix's investor page usually returned its navigation shell without the
requested financial material; one proxy received a challenge immediately.

| Route label | DuckDuckGo results / 6 | Median time for the nine accessible source URLs |
| --- | ---: | ---: |
| Direct | 2 | 0.18 s |
| UK 1 | 4 | 0.90 s |
| UK 2 | 0 | 1.00 s |
| UK 3 | 6 | 0.94 s |
| Spain | 2 | 1.10 s |
| US 1 | 6 | 0.72 s |
| US 2 | 4 | 0.24 s |
| Poland | 4 | 1.44 s |
| Japan | 2 | 1.74 s |
| US 3 | 3 | 0.97 s |
| Germany | 6 | 1.00 s |

These timings include fetching the response body. They describe this workload
and observation window, with different geographic routes and cache conditions.

## Browser and repeat checks

Headless Chromium visited one Google query, one DuckDuckGo query, ACM and Netflix
over the direct route and the UK 1, US 1 and Japan proxies: **16 navigations**.
JavaScript was enabled; images, media and fonts were disabled to limit transfer.
All sixteen reached challenge pages. No challenge was solved. Google and
DuckDuckGo challenges and the ACM/Netflix verification pages were retained as
screenshots. A browser did not resolve these cases under the tested settings.

Five endpoints were then requested twice over every route, adding **110 HTTP
requests**. Across the proxies, Bing, Google Scholar and Apple succeeded in all
20 requests apiece. Google again supplied no plain-HTTP results. DuckDuckGo
produced 13 result pages, six challenges and one TLS connection error in its 20
proxied requests. Browser and HTTP differences should not be attributed to
IP reputation alone: their client behavior and cookies differ.

The practical finding is that these proxies provide independent exits and
support many of this sample's research requests. Search and protected-site
access remains uneven. The test used no paid search API, existing site login,
CAPTCHA solver or large-scale load generator.

## SDK integration

Sandweave 0.2.9 adds `Network(proxy=url_or_list)`. Each sandbox binds one proxy
for its lifetime. The network policy outside the guest permits only that
endpoint; commands receive standard proxy environment variables. Applications
that ignore those variables need explicit proxy configuration. Arbitrary direct
sockets are blocked rather than silently using the worker's IP.

Live SDK checks verified separate exit addresses in concurrent sandboxes,
blocked direct-IP and direct-DNS requests, continued SDK control access, proxied
setup, live restoration with the same proxy, and filesystem-cache restoration
with another proxy or offline networking. Credentials are absent from public
endpoint summaries and this report. Private credential files, worker records,
snapshots and raw probe bodies remain outside Git.

The complete request lists and aggregate results are committed. Local bodies,
browser screenshots and per-request logs are under `runs/proxy-quest/`.
Reproduce with `scripts/probe-research-proxies.py`; the `sample`, `run` and
`browser` subcommands show their required input paths with `--help`.
To sample the same dataset version, pass
`--revision 6003ea03bfa41b54d76ad7abe486ba41fa3e0d01 --seed 20260911`.
