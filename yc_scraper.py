#!/usr/bin/env python3
"""Scrape YC companies (Summer 2026 + Fall 2026): company desc, socials, founders, emails.

Stdlib only. Usage: python3 yc_scraper.py [--batches "Summer 2026" "Fall 2026"] [--out yc_founders]
Outputs <out>.json (nested) and <out>.csv (one row per founder).

Emails: YC doesn't publish founder emails. We collect emails found on the company
website (homepage + common contact pages) and add pattern guesses (first@, first.last@,
flast@) for each founder, marked as guesses in `email_guesses`.
"""
import argparse, concurrent.futures as cf, csv, html, json, re, socket, ssl, sys, time, unicodedata
import urllib.parse, urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')  # Cloudflare's "email protection" obfuscation
BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|sentry|wixpress|example\.|@2x|u00|domain\.com|email\.com", re.I)
CONTACT_PATHS = ["", "/contact", "/about", "/team", "/contact-us", "/about-us"]
CTX = ssl.create_default_context()


def get(url, timeout=15, data=None, headers=None):
    h = {"User-Agent": UA, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read(2_000_000).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            return None


def algolia_key():
    page = get("https://www.ycombinator.com/companies")
    m = re.search(r'window\.AlgoliaOpts = \{"app":"(\w+)","key":"([^"]+)"', page or "")
    if not m:
        sys.exit("Could not find Algolia key on YC companies page")
    return m.group(1), m.group(2)


def list_companies(batches):
    app, key = algolia_key()
    url = f"https://{app}-dsn.algolia.net/1/indexes/YCCompany_production/query"
    facet = json.dumps([[f"batch:{b}" for b in batches]])
    hits, page = [], 0
    while True:
        params = urllib.parse.urlencode({"hitsPerPage": 1000, "page": page, "facetFilters": facet})
        body = json.dumps({"params": params}).encode()
        res = json.loads(get(url, data=body, headers={"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key}))
        hits += res["hits"]
        page += 1
        if page >= res["nbPages"]:
            return hits


def company_details(slug):
    page = get(f"https://www.ycombinator.com/companies/{slug}")
    m = re.search(r'data-page="([^"]*)"', page or "")
    return json.loads(html.unescape(m.group(1)))["props"]["company"] if m else {}


def domain_of(website):
    host = urllib.parse.urlparse(website if "//" in (website or "") else f"https://{website}").hostname or ""
    return host.removeprefix("www.").lower()


def cf_decode(hexstr):
    # Cloudflare's "email protection" shows "[email protected]" on the page and stashes the real
    # address XOR-masked in a data-cfemail="<hex>" attribute: byte 0 is the key, every later byte
    # is a masked character. Very common on small-business sites (Webflow/WordPress + Cloudflare);
    # EMAIL_RE alone never sees these addresses, so without this they silently fall back to a guess.
    try:
        key, rest = int(hexstr[:2], 16), hexstr[2:]
        if len(rest) < 2 or len(rest) % 2:
            return None
        return bytes(int(rest[i : i + 2], 16) ^ key for i in range(0, len(rest), 2)).decode("ascii")
    except ValueError:
        return None


def site_emails(website, domain):
    if not website:
        return []
    base = website.rstrip("/")
    found = set()
    for path in CONTACT_PATHS:
        raw = get(base + path, timeout=10)
        if not raw:
            continue
        unescaped = urllib.parse.unquote(html.unescape(raw))
        candidates = EMAIL_RE.findall(unescaped)
        candidates += filter(None, (cf_decode(h) for h in CF_EMAIL_RE.findall(raw)))
        for e in candidates:
            e = e.lower().strip(".")
            # Only keep emails on the company's own domain (drops form placeholders like you@company.com)
            host = e.split("@")[-1]
            if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e) and (host == domain or host.endswith("." + domain)):
                found.add(e)
    return sorted(found)


def ascii_name(s):
    return re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()).split()


def domain_resolves(domain):
    try:
        socket.getaddrinfo(domain, None)
        return True
    except Exception:
        return False


def guesses(full_name, domain):
    parts = ascii_name(full_name)
    if not parts or not domain:
        return []
    first, last = parts[0], parts[-1] if len(parts) > 1 else ""
    g = [f"{first}@{domain}"]
    if last:
        g += [f"{first}.{last}@{domain}", f"{first[0]}{last}@{domain}", f"{first}{last}@{domain}"]
    return g


def process(hit):
    slug = hit["slug"]
    c = company_details(slug)
    website = c.get("website") or hit.get("website") or ""
    domain = domain_of(website)
    emails = site_emails(website, domain)
    resolves = domain_resolves(domain) if domain else False
    founders = []
    for f in c.get("founders", []):
        name = f.get("full_name", "")
        first = (ascii_name(name) or [""])[0]
        # Emails from the site that look like this founder's
        matched = [e for e in emails if first and e.split("@")[0].startswith(first)]
        founders.append({
            "name": name,
            "title": f.get("title"),
            "bio": f.get("founder_bio"),
            "linkedin": f.get("linkedin_url"),
            "twitter": f.get("twitter_url"),
            "emails_found": matched,
            "email_guesses": guesses(name, domain) if resolves else [],
        })
    return {
        "name": hit["name"],
        "batch": hit.get("batch"),
        "yc_url": f"https://www.ycombinator.com/companies/{slug}",
        "website": website,
        "one_liner": hit.get("one_liner"),
        "description": c.get("long_description") or hit.get("long_description"),
        "industry": hit.get("subindustry"),
        "tags": hit.get("tags"),
        "location": hit.get("all_locations"),
        "team_size": hit.get("team_size"),
        "launched_at": hit.get("launched_at"),
        "linkedin": c.get("linkedin_url"),
        "twitter": c.get("twitter_url"),
        "github": c.get("github_url"),
        "crunchbase": c.get("cb_url"),
        "facebook": c.get("fb_url"),
        "site_emails": emails,
        "founders": founders,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", nargs="+", default=["Summer 2026", "Fall 2026"])
    ap.add_argument("--out", default="yc_founders")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    hits = list_companies(a.batches)
    if a.limit:
        hits = hits[: a.limit]
    print(f"{len(hits)} companies", file=sys.stderr)

    results = []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(process, hits), 1):
            results.append(r)
            print(f"[{i}/{len(hits)}] {r['name']}: {len(r['founders'])} founders, {len(r['site_emails'])} site emails", file=sys.stderr)

    with open(f"{a.out}.json", "w") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)

    cols = ["company", "batch", "website", "yc_url", "one_liner", "description", "industry", "location", "team_size",
            "company_linkedin", "company_twitter", "company_github", "site_emails",
            "founder", "founder_title", "founder_linkedin", "founder_twitter", "founder_emails_found", "founder_email_guesses"]
    with open(f"{a.out}.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in results:
            base = [r["name"], r["batch"], r["website"], r["yc_url"], r["one_liner"], r["description"], r["industry"],
                    r["location"], r["team_size"], r["linkedin"], r["twitter"], r["github"], "; ".join(r["site_emails"])]
            for f in r["founders"] or [{}]:
                w.writerow(base + [f.get("name"), f.get("title"), f.get("linkedin"), f.get("twitter"),
                                   "; ".join(f.get("emails_found", [])), "; ".join(f.get("email_guesses", []))])
    print(f"Wrote {a.out}.json and {a.out}.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
