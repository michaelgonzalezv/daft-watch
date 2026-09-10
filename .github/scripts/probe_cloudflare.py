"""Spike: can headless Chromium clear daft.ie's Cloudflare from a GitHub
Actions runner (datacenter IP)? Fetches one search page and reports.

Exit 0 = got listings. Exit 1 = Cloudflare blocked. Exit 2 = other error.
"""

import sys

from daftwatch.adapter import AdapterError, RateLimited, _default_client


def main() -> int:
    client = _default_client()
    try:
        client.set_category("sharing")
        client.set_params({"location": ["cork-city"]})
        page = client.page(1)
        print(f"OK — {len(page)} listings from /sharing/cork-city")
        if page:
            first = page[0]
            print(f"  sample: {first.get('title')!r} {first.get('price')!r}")
        return 0
    except RateLimited as exc:
        print(f"BLOCKED — Cloudflare challenge, no __NEXT_DATA__: {exc}")
        return 1
    except AdapterError as exc:
        print(f"ADAPTER ERROR: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 2
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
