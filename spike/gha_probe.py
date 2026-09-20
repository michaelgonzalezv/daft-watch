"""Throwaway probe — NOT part of daft-watch, lives only on the spike/us-rooms-http branch.

Question it answers: does a GitHub Actions runner get 200s from Zumper and
Craigslist, or does it get blocked? Same conservative approach as the local
spike: identifiable User-Agent, >= 2 s between requests to a host, and on any
403 / 429 / captcha marker the source is logged and STOPPED — no retry, no
workaround. Uses no secrets and writes nothing outside the results artifact.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

UA = "daft-watch-spike/0.1 (personal research; +https://github.com/michaelgonzalezv/daft-watch)"
DELAY = 2.0
TIMEOUT = 30
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gha_probe_results.json")

BLOCK = re.compile(r"px-captcha|perimeterx|press (?:&amp;|&|and) hold|just a moment|attention required|"
                   r"captcha-delivery|datadome|verify you are (?:a )?human|access denied", re.I)

ZUMPER = {"nyc": "new-york-ny", "miami": "miami-fl", "chicago": "chicago-il", "austin": "austin-tx"}
CL = {"nyc": "newyork", "miami": "miami", "chicago": "chicago", "austin": "austin"}
CL_DETAILS_PER_METRO = 2

_last = {}
_stopped = set()
results = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ua": UA, "requests": [], "zumper": {}, "craigslist": {}}


def get(url, source):
    """One polite GET. Returns (status, text). Records everything; never raises."""
    if source in _stopped:
        results["requests"].append({"source": source, "url": url, "skipped": "source stopped after a block"})
        return None, ""
    host = url.split("/")[2]
    wait = DELAY - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    t0 = time.time()
    status, text, err = 0, "", ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, text = r.status, r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            text = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    _last[host] = time.time()
    elapsed = round(time.time() - t0, 2)
    marker = BLOCK.search(text[:6000])
    blocked = status in (403, 429) or bool(marker)
    results["requests"].append({"source": source, "url": url, "status": status, "elapsed_s": elapsed, "bytes": len(text),
                                "block_marker": marker.group(0) if marker else None, "blocked": blocked, "error": err})
    print(f"{source:11} {status} {elapsed:5.2f}s {len(text):8}B {'BLOCKED ' + (marker.group(0) if marker else '') if blocked else ''} {url}", flush=True)
    if blocked:
        _stopped.add(source)          # leave it alone: no retry, no workaround
    return status, text


def zumper_state(html):
    i = html.find("window.__PRELOADED_STATE__")
    if i < 0:
        return None
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*", html[i:i + 200])
    try:
        st, _ = json.JSONDecoder().raw_decode(html[i + m.end():])
        return st["currentSearch"]["listables"]
    except Exception:
        return None


def zumper_metro(metro, slug, max_pages):
    ids_by_page, pages = [], []
    for page in range(1, max_pages + 1):
        url = f"https://www.zumper.com/rooms-for-rent/{slug}" + ("" if page == 1 else f"?page={page}")
        status, text = get(url, "zumper")
        if status != 200:
            pages.append({"page": page, "status": status}); break
        ls = zumper_state(text)
        if ls is None:
            pages.append({"page": page, "status": status, "parse": "no state"}); break
        L = ls.get("listables") or []
        ids = [x["listing_id"] for x in L]
        ids_by_page.append(ids)
        pages.append({"page": page, "status": status, "n": len(L), "listingCount": ls.get("listingCount"), "moreResult": ls.get("moreResult")})
        if not L or ls.get("moreResult") is False:
            break
    flat = [i for ids in ids_by_page for i in ids]
    return {"pages": pages, "unique_ids": len(set(flat)), "sum_of_pages": len(flat), "ids_page1": ids_by_page[0] if ids_by_page else [],
            "ids_page2": ids_by_page[1] if len(ids_by_page) > 1 else []}


def cl_search(sub):
    status, text = get(f"https://{sub}.craigslist.org/search/roo", "craigslist")
    if status != 200:
        return {"status": status, "rows": 0, "links": []}
    nodes = re.findall(r'<li class="cl-static-search-result"[^>]*>.*?</li>', text, re.S)
    links = [re.search(r'href="([^"]+)"', n).group(1) for n in nodes if re.search(r'href="([^"]+)"', n)]
    ld = 0
    for b in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(b)
            if d.get("itemListElement"):
                ld = len(d["itemListElement"])
        except Exception:
            pass
    return {"status": status, "rows": len(nodes), "ld_items": ld, "links": links}


def cl_detail(url):
    status, text = get(url, "craigslist")
    if status != 200:
        return {"status": status}
    body = re.search(r'<section id="postingbody">(.*?)</section>', text, re.S)
    mp = re.search(r'id="map"[^>]*data-latitude="([^"]+)"[^>]*data-longitude="([^"]+)"', text)
    times = re.findall(r'<time[^>]*datetime="([^"]+)"', text)
    return {"status": status, "has_body": bool(body), "body_chars": len(re.sub(r"<[^>]+>", "", body.group(1))) if body else 0,
            "has_coords": bool(mp), "posted": times[0] if times else None}


def main():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            results["runner_ip"] = r.read().decode().strip()
    except Exception as e:
        results["runner_ip"] = f"unknown ({e})"
    # ---- Zumper: NYC paginated in full, the other metros' first page, then a repeat of NYC pages 1-2 for stability
    results["zumper"]["nyc"] = zumper_metro("nyc", ZUMPER["nyc"], 12)
    for m in ("miami", "chicago", "austin"):
        results["zumper"][m] = zumper_metro(m, ZUMPER[m], 2)
    rep = zumper_metro("nyc-repeat", ZUMPER["nyc"], 2)
    first = results["zumper"]["nyc"]
    results["zumper"]["nyc_repeat_check"] = {
        "page1_same_ids": rep["ids_page1"] == first["ids_page1"], "page2_same_ids": rep["ids_page2"] == first["ids_page2"],
        "page1_overlap": len(set(rep["ids_page1"]) & set(first["ids_page1"])), "page2_overlap": len(set(rep["ids_page2"]) & set(first["ids_page2"])),
        "repeat_pages": rep["pages"]}
    # ---- Craigslist: the 4 search pages, then 2 posting pages per metro
    for m, sub in CL.items():
        s = cl_search(sub)
        links = s.pop("links")
        results["craigslist"][m] = {"search": s, "details": [cl_detail(u) for u in (links[1], links[4]) if len(links) > 4][:CL_DETAILS_PER_METRO]}
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results["stopped_sources"] = sorted(_stopped)
    json.dump(results, open(OUT, "w"), indent=1)
    print("done; stopped sources:", sorted(_stopped) or "none", flush=True)


if __name__ == "__main__":
    main()
