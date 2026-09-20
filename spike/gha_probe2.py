"""Throwaway inventory-movement probe — NOT part of daft-watch; lives only on spike/us-rooms-http.

One run = one snapshot of Zumper and Craigslist (4 metros) written to
spike/data/snap_<slot>_<timestamp>.json, plus a small accessibility sample.
It only OBSERVES: an ad missing from a snapshot is recorded as "absent", never
as GONE — deciding what absence means is exactly what this experiment is for.

Same conditions as the first probe: identifiable User-Agent, >= 2 s between
requests to a host, no secrets. On any 403 / 429 / captcha marker the source is
recorded and STOPPED for the rest of the experiment (state.json) — no retry,
no workaround; the owner decides what happens next.
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("SPIKE_DATA_DIR") or os.path.join(HERE, "data")
UA = "daft-watch-spike/0.1 (personal research; +https://github.com/michaelgonzalezv/daft-watch)"
DELAY, TIMEOUT = 2.0, 30
BLOCK = re.compile(r"px-captcha|perimeterx|press (?:&amp;|&|and) hold|just a moment|attention required|"
                   r"captcha-delivery|datadome|verify you are (?:a )?human|access denied", re.I)
ZUMPER = {"nyc": "new-york-ny", "miami": "miami-fl", "chicago": "chicago-il", "austin": "austin-tx"}
CL = {"nyc": "newyork", "miami": "miami", "chicago": "chicago", "austin": "austin"}
ABSENT_CHECKS_PER_METRO = 3
DETAIL_SAMPLE_PER_METRO = 2

os.makedirs(DATA, exist_ok=True)
STATE = os.path.join(DATA, "state.json")
state = json.load(open(STATE)) if os.path.exists(STATE) else {"stopped": {}}
snap = {"slot": int(sys.argv[1]) if len(sys.argv) > 1 else 0, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ua": UA, "requests": [], "zumper": {}, "craigslist": {}}
_last, _stopped_now = {}, set()


def get(url, source):
    if source in state["stopped"] or source in _stopped_now:
        snap["requests"].append({"source": source, "url": url, "skipped": "source stopped"})
        return None, "", url
    host = url.split("/")[2]
    wait = DELAY - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    t0 = time.time()
    status, text, err, final = 0, "", "", url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, text, final = r.status, r.read().decode("utf-8", errors="ignore"), r.geturl()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            text = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    _last[host] = time.time()
    marker = BLOCK.search(text[:6000])
    blocked = status in (403, 429) or bool(marker)
    snap["requests"].append({"source": source, "url": url, "status": status, "elapsed_s": round(time.time() - t0, 2),
                             "bytes": len(text), "marker": marker.group(0) if marker else None, "blocked": blocked, "error": err})
    print(f"{source:10} {status} {time.time()-t0:5.2f}s {len(text):8}B {'BLOCKED' if blocked else ''} {url}", flush=True)
    if blocked:
        _stopped_now.add(source)
        state["stopped"][source] = {"at": snap["started"], "status": status, "marker": marker.group(0) if marker else None, "url": url}
    return status, text, final


def zstate(html):
    i = html.find("window.__PRELOADED_STATE__")
    if i < 0:
        return None
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*", html[i:i + 200])
    try:
        return json.JSONDecoder().raw_decode(html[i + m.end():])[0]["currentSearch"]["listables"]
    except Exception:
        return None


def capture_zumper(metro, slug):
    out = {"pages": [], "records": {}, "dup_ids_across_pages": 0}
    for page in range(1, 13 if metro == "nyc" else 3):
        url = f"https://www.zumper.com/rooms-for-rent/{slug}" + ("" if page == 1 else f"?page={page}")
        status, text, _ = get(url, "zumper")
        if status != 200:
            out["pages"].append({"page": page, "status": status}); break
        ls = zstate(text)
        if ls is None:
            out["pages"].append({"page": page, "status": status, "parse": "no state"}); break
        L = ls.get("listables") or []
        out["pages"].append({"page": page, "status": status, "n": len(L), "listingCount": ls.get("listingCount"), "moreResult": ls.get("moreResult"), "bytes": len(text)})
        for pos, x in enumerate(L):
            k = str(x["listing_id"])
            if k in out["records"]:
                out["dup_ids_across_pages"] += 1
                continue
            out["records"][k] = {"p": x.get("min_price"), "pm": x.get("max_price"), "st": x.get("listing_status"), "mod": x.get("modified_on"),
                                 "lst": x.get("listed_on"), "pg": page, "pos": pos, "op": x.get("brokerage_name"), "prev": x.get("previous_price")}
        if not L or ls.get("moreResult") is False:
            break
    return out


def capture_cl(metro, sub):
    status, text, _ = get(f"https://{sub}.craigslist.org/search/roo", "craigslist")
    out = {"status": status, "rows": [], "details": [], "absent_checks": []}
    if status != 200:
        return out
    ld = 0
    for b in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(b)
            if d.get("itemListElement"):
                ld = len(d["itemListElement"])
        except Exception:
            pass
    out["ld_items"], out["bytes"] = ld, len(text)
    for n in re.findall(r'<li class="cl-static-search-result"[^>]*>.*?</li>', text, re.S):
        href = re.search(r'href="([^"]+)"', n)
        if not href:
            continue
        t = re.search(r'<div class="title">(.*?)</div>', n, re.S)
        p = re.search(r'<div class="price">\s*(.*?)\s*</div>', n, re.S)
        loc = re.search(r'<div class="location">\s*(.*?)\s*</div>', n, re.S)
        out["rows"].append({"id": href.group(1).rstrip("/").split("/")[-1], "url": href.group(1),
                            "price": p.group(1).strip() if p else None,
                            "title": re.sub(r"\s+", " ", t.group(1)).strip()[:70] if t else None,
                            "loc": re.sub(r"\s+", " ", loc.group(1)).strip() if loc else None})
    return out


def cl_detail_info(url):
    status, text, final = get(url, "craigslist")
    if status is None:
        return {"url": url, "skipped": True}
    body = re.search(r'<section id="postingbody">(.*?)</section>', text, re.S)
    times = re.findall(r'<time[^>]*datetime="([^"]+)"', text)
    return {"url": url, "status": status, "final_url_same": final == url, "has_body": bool(body),
            "body_chars": len(re.sub(r"<[^>]+>", "", body.group(1))) if body else 0,
            "has_coords": bool(re.search(r'id="map"[^>]*data-latitude', text)), "posted": times[0] if times else None,
            "gone_marker": bool(re.search(r"has been deleted|flagged for removal|this posting (?:has )?expired|no longer available", text, re.I)),
            "title": (re.search(r"<title>(.*?)</title>", text, re.S).group(1)[:80] if re.search(r"<title>", text) else None)}


def previous_snapshot():
    files = sorted(glob.glob(os.path.join(DATA, "snap_*.json")))
    return json.load(open(files[-1])) if files else None


def main():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            snap["runner_ip"] = r.read().decode().strip()
    except Exception as e:
        snap["runner_ip"] = f"unknown ({e})"
    prev = previous_snapshot()
    snap["previous_slot"] = prev["slot"] if prev else None
    for m, slug in ZUMPER.items():
        snap["zumper"][m] = capture_zumper(m, slug)
    for m, sub in CL.items():
        c = capture_cl(m, sub)
        rows = c["rows"]
        if len(rows) > 4:
            c["details"] = [cl_detail_info(rows[i]["url"]) for i in (1, 4)][:DETAIL_SAMPLE_PER_METRO]
        # what does a post that vanished from the search list look like when opened?
        if prev and prev["craigslist"].get(m, {}).get("rows") and rows:
            now_ids = {r["id"] for r in rows}
            gone = [r for r in prev["craigslist"][m]["rows"] if r["id"] not in now_ids][:ABSENT_CHECKS_PER_METRO]
            c["absent_checks"] = [{"id": g["id"], **cl_detail_info(g["url"])} for g in gone]
        snap["craigslist"][m] = c
    snap["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    snap["stopped_sources"] = sorted(state["stopped"])
    stamp = time.strftime("%Y%m%dT%H%MZ", time.gmtime())
    path = os.path.join(DATA, f"snap_{snap['slot']}_{stamp}.json")
    json.dump(snap, open(path, "w"), separators=(",", ":"))
    json.dump(state, open(STATE, "w"), indent=1)
    # quick log (real analysis happens offline across all snapshots)
    z = {m: len(v["records"]) for m, v in snap["zumper"].items()}
    c = {m: len(v["rows"]) for m, v in snap["craigslist"].items()}
    print(f"slot {snap['slot']} zumper ids {z} craigslist rows {c} stopped {sorted(state['stopped'])}", flush=True)
    if {"zumper", "craigslist"} <= set(state["stopped"]):
        print("both sources stopped: ending the chain", flush=True)
        sys.exit(3)


if __name__ == "__main__":
    main()
