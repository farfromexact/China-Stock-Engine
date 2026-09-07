"""Inspect public SuperCommand metadata routes; no authentication or data calls.

This helper prints only bounded public catalog route snippets, never a user
session, credentials, market response or the entire downloaded application.
"""
import re
from urllib.request import urlopen


def main():
    page_url = "https://quantapi.51ifind.com/gwstatic/static/ds_web/super-command-web/index.html"
    with urlopen(page_url, timeout=30) as response:
        page = response.read().decode("utf-8")
    urls = re.findall(r'(?:src|href)="(//s\.thsi\.cn/[^" ]+/assets/index-[^" ]+\.js)"', page)
    if not urls:
        raise SystemExit("public SuperCommand asset link unavailable")
    with urlopen("https:" + urls[0], timeout=30) as response:
        source = response.read(2_000_000).decode("utf-8")
    snippets = re.findall(r'.{0,30}(?:Et|ma)\("/[^"\s]+".{0,100}', source)
    for snippet in snippets:
        if any(word in snippet.lower() for word in ("index", "indicator", "search", "tree", "config", "param")):
            print(snippet)
    print("Public application bytes:", len(source.encode("utf-8")))


if __name__ == "__main__":
    main()
