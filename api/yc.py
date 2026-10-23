"""Vercel serverless function: YC batches, companies, founders, jobs and a change digest. Stdlib only, no API key.

GET /api/yc?action=batches
GET /api/yc?action=companies&batch=Winter%202024[,Summer%202024,...]   (multiple batches comma-separated)
GET /api/yc?action=founders&slugs=airbnb,stripe        (max 10 slugs per call)
GET /api/yc?action=job&url=https://www.ycombinator.com/companies/stripe/jobs/<id>
GET /api/yc?action=digest&known_batches=<json>&slugs=a,b,c&known_jobs=<json>

Founder names and job descriptions only live on each company's/job's ycombinator.com page, which
browsers can't fetch cross-origin, so this runs server-side. Emails: addresses on the company site
plus pattern guesses, ranked by a confidence score and an MX-record check. Optional verified lookup
(Apify) runs in the browser, so visitors' tokens never reach this server.
"""
import concurrent.futures as cf, html, json, re, socket, ssl, time, unicodedata, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')  # Cloudflare's "email protection" obfuscation
BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|sentry|wixpress|example\.|@2x|u00|domain\.com|email\.com", re.I)
SLUG_RE = re.compile(r"^[a-z0-9-]{1,100}$")
BATCH_RE = re.compile(r"^(Winter|Spring|Summer|Fall) \d{4}$|^[WSFX]\d{2}$|^IK12$|^Unspecified$")
JOB_URL_RE = re.compile(r"^https://(www\.)?ycombinator\.com/companies/[a-z0-9-]{1,100}/jobs/[A-Za-z0-9_-]{1,200}(?:[/?].*)?$")
MAX_SLUGS = 10
MAX_BATCHES = 8  # multi-batch search: sanity cap, same spirit as MAX_SLUGS
MAX_DIGEST_SLUGS = 40  # digest only checks job counts (no site scrape), so it can cover more than MAX_SLUGS
CTX = ssl.create_default_context()
SEASON = {"Winter": 1, "Spring": 2, "Summer": 3, "Fall": 4}
# Confidence (%) that a given pattern guess is correct, taken from this project's own benchmark
# (README: first guess right 80% of the time, correct address somewhere in the list 93% of the time).
GUESS_CONFIDENCE = {"first": 80, "first.last": 7, "flast": 4, "firstlast": 2}
GUESS_CONFIDENCE_NO_LAST = {"first": 55}  # no last name on file: only one guess is possible, less certain
INFERRED_CONFIDENCE = 95  # a same-domain founder already confirmed this exact pattern
INFERRED_OTHER_CAP = 3  # once a pattern is inferred, every other guess for that domain is capped this low
MX_UNKNOWN_FACTOR = 0.75  # DNS-over-HTTPS lookup failed/timed out: still offer guesses, less sure of them
_algolia = None  # (app, key), cached per warm instance
_pool = cf.ThreadPoolExecutor(32)  # site checks; outlives a request so a slow site can be abandoned
SITE_DEADLINE = 4  # seconds per company before giving up on its website


def get(url, timeout=8, data=None, headers=None):
    # ponytail: one attempt, short timeout; the CLI (yc_scraper.py) retries, a web request can't wait
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.read(2_000_000).decode("utf-8", "ignore")
    except Exception:
        return None


def algolia(params):
    global _algolia
    if not _algolia:
        m = re.search(r'window\.AlgoliaOpts = \{"app":"(\w+)","key":"([^"]+)"', get("https://www.ycombinator.com/companies") or "")
        if not m:
            raise RuntimeError("Could not read YC's company search")
        _algolia = m.group(1), m.group(2)
    app, key = _algolia
    body = json.dumps({"params": urllib.parse.urlencode(params)}).encode()
    for host in (f"{app}-dsn.algolia.net", f"{app}-1.algolianet.com", f"{app}-2.algolianet.com"):  # Algolia's fallback hosts
        res = get(f"https://{host}/1/indexes/YCCompany_production/query", data=body,
                  headers={"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key})
        if res is not None:
            return json.loads(res)
    _algolia = None  # key may have rotated
    raise RuntimeError("YC company search failed")


def batch_key(b):
    p = b.split()
    return (int(p[1]), SEASON.get(p[0], 0)) if len(p) == 2 and p[1].isdigit() else (0, 0)


def batches():
    facets = algolia({"hitsPerPage": 0, "facets": json.dumps(["batch"]), "maxValuesPerFacet": 1000})["facets"]["batch"]
    return [{"batch": b, "count": n} for b, n in sorted(facets.items(), key=lambda x: batch_key(x[0]), reverse=True)]


def companies_one(batch):
    hits, page = [], 0
    while True:
        res = algolia({"hitsPerPage": 1000, "page": page, "facetFilters": json.dumps([[f"batch:{batch}"]])})
        hits += res["hits"]
        page += 1
        if page >= res["nbPages"]:
            break
    return [{"name": h["name"], "slug": h["slug"], "batch": h.get("batch"), "website": h.get("website") or "",
             "one_liner": h.get("one_liner") or "", "industry": h.get("subindustry") or "", "team_size": h.get("team_size"),
             "launched_at": h.get("launched_at") or 0} for h in hits]


def companies(batch_list):
    # Multi-batch search: fetch each batch (in parallel) and merge, de-duplicating by slug in case
    # YC ever lists a company under more than one batch value.
    seen, out = set(), []
    with cf.ThreadPoolExecutor(max(1, len(batch_list))) as ex:
        for rows in ex.map(companies_one, batch_list):
            for r in rows:
                if r["slug"] not in seen:
                    seen.add(r["slug"])
                    out.append(r)
    return out


def domain_of(website):
    host = urllib.parse.urlparse(website if "//" in (website or "") else f"https://{website}").hostname or ""
    return host.removeprefix("www.").lower()


def ascii_name(s):
    return re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()).split()


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
    found = set()
    for path in ("", "/contact"):  # ponytail: 2 pages, not the CLI's 6, to fit in a function timeout
        raw = get(website.rstrip("/") + path, timeout=3) or ""
        candidates = EMAIL_RE.findall(urllib.parse.unquote(html.unescape(raw)))
        candidates += filter(None, (cf_decode(h) for h in CF_EMAIL_RE.findall(raw)))
        for e in candidates:
            e = e.lower().strip(".")
            host = e.split("@")[-1]
            if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e) and (host == domain or host.endswith("." + domain)):
                found.add(e)
    return sorted(found)


def guesses(full_name, domain, factor=1.0):
    # Returns [{email, pattern, confidence}], highest confidence first. `factor` scales confidence
    # down (e.g. when the MX lookup was inconclusive) without changing the ranking.
    parts = ascii_name(full_name)
    if not parts or not domain:
        return []
    first, last = parts[0], parts[-1] if len(parts) > 1 else ""
    if not last:
        pats = {"first": first}
        base = GUESS_CONFIDENCE_NO_LAST
    else:
        pats = {"first": first, "first.last": f"{first}.{last}", "flast": f"{first[0]}{last}", "firstlast": f"{first}{last}"}
        base = GUESS_CONFIDENCE
    return [{"email": f"{pats[p]}@{domain}", "pattern": p, "confidence": round(base[p] * factor)} for p in pats]


def infer_pattern(local_part, full_name):
    # Does an already-known real email's local part match one of our own guess patterns for this
    # person? If so, every other founder at the same domain almost certainly uses that same pattern.
    parts = ascii_name(full_name)
    if not parts:
        return None
    first, last = parts[0], parts[-1] if len(parts) > 1 else ""
    if local_part == first:
        return "first"
    if last and local_part == f"{first}.{last}":
        return "first.last"
    if last and local_part == f"{first[0]}{last}":
        return "flast"
    if last and local_part == f"{first}{last}":
        return "firstlast"
    return None


def apply_inferred_pattern(out):
    # Mutates `out` (list of founder dicts for one company) in place: once any founder's real email
    # (site_emails) reveals this company's address pattern, re-rank every other founder's guesses so
    # that same pattern comes first with high confidence, instead of leaving them on the generic
    # frequency-based order.
    inferred = None
    for f in out:
        for e in f["emails_found"]:
            inferred = infer_pattern(e.split("@")[0], f["name"])
            if inferred:
                break
        if inferred:
            break
    if not inferred:
        return
    for f in out:
        if f["emails_found"] or not f["email_guesses"]:
            continue
        match = next((g for g in f["email_guesses"] if g["pattern"] == inferred), None)
        if not match:
            continue
        match["confidence"] = INFERRED_CONFIDENCE
        others = [g for g in f["email_guesses"] if g is not match]
        for g in others:
            g["confidence"] = min(g["confidence"], INFERRED_OTHER_CAP)
        f["email_guesses"] = [match] + others


def resolves(domain):
    try:
        socket.getaddrinfo(domain, None)
        return True
    except Exception:
        return False


def resolves_mx(domain):
    # DNS-over-HTTPS (Cloudflare's resolver) instead of a dnspython dependency. Returns True (has a
    # real mail server), False (confirmed no mail server, including an RFC 7505 null MX "0 ."), or
    # None if the lookup itself failed/timed out (caller should treat that as "unknown", not "no").
    raw = get(f"https://cloudflare-dns.com/dns-query?name={urllib.parse.quote(domain)}&type=MX",
              headers={"Accept": "application/dns-json"}, timeout=4)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if data.get("Status") != 0:
        return None
    mx = [a for a in data.get("Answer") or [] if a.get("type") == 15]
    if not mx:
        return False
    if all(a.get("data", "").strip().rstrip(".").strip() == "0" for a in mx):
        return False  # null MX: domain explicitly accepts no mail
    return True


def open_jobs(props):
    # jobPostings is on the same company page as the founders, next to props.company
    jobs = []
    for j in props.get("jobPostings") or []:
        if not isinstance(j, dict) or j.get("isIncomplete"):
            continue
        path = j.get("url") or ""
        jobs.append({"title": j.get("title") or "Open role",
                     "url": "https://www.ycombinator.com" + path if path.startswith("/") else path})
    return jobs


def founders(slug):
    page = get(f"https://www.ycombinator.com/companies/{slug}")
    m = re.search(r'data-page="([^"]*)"', page or "")
    if not m:
        return {"slug": slug, "error": "Couldn't load this company's YC page"}
    props = json.loads(html.unescape(m.group(1)))["props"]
    c = props["company"]
    jobs = open_jobs(props)
    website = c.get("website") or ""  # always from YC, never from the client
    domain = domain_of(website)
    try:
        emails = _pool.submit(site_emails, website, domain).result(timeout=SITE_DEADLINE) if domain else []
    except cf.TimeoutError:
        emails = []  # ponytail: slow site skipped, guesses still cover it; the CLI waits longer
    dns = resolves(domain) if domain else False
    mx = resolves_mx(domain) if dns else None  # None = DNS resolved but MX lookup itself was inconclusive
    # mx is False only when the lookup *succeeded* and confirmed there is no mail server: don't offer
    # guesses at all then. mx is None when the lookup failed/timed out: still guess, just less sure.
    offer_guesses = dns and mx is not False
    factor = 1.0 if mx else MX_UNKNOWN_FACTOR
    out = []
    for f in c.get("founders", []):
        name = f.get("full_name") or ""
        first = (ascii_name(name) or [""])[0]
        out.append({"name": name, "title": f.get("title") or "", "linkedin": f.get("linkedin_url") or "",
                    "twitter": f.get("twitter_url") or "",
                    "emails_found": [e for e in emails if first and e.split("@")[0].startswith(first)],
                    "email_guesses": guesses(name, domain, factor) if offer_guesses else []})
    apply_inferred_pattern(out)
    return {"slug": slug, "website": website, "domain": domain, "linkedin": c.get("linkedin_url") or "",
            "twitter": c.get("twitter_url") or "", "site_emails": emails, "mx": mx, "job_count": len(jobs),
            "jobs": jobs, "founders": out}


def job_detail(url):
    page = get(url)
    m = re.search(r'data-page="([^"]*)"', page or "")
    if not m:
        return {"error": "Couldn't load this job's YC page"}
    props = json.loads(html.unescape(m.group(1)))["props"]
    j = props.get("job") or {}
    if not j:
        return {"error": "This job posting couldn't be read"}
    return {"title": j.get("title") or "", "url": url, "location": j.get("location") or "",
            "type": j.get("type") or "", "role": j.get("prettyRole") or j.get("role") or "",
            "min_experience": j.get("minExperience") or "", "salary_range": j.get("salaryRange") or "",
            "equity_range": j.get("equityRange") or "", "visa": j.get("visa") or "",
            "skills": j.get("skills") or [], "description": (j.get("description") or "").strip(),
            "interview_process": (j.get("interview_process") or "").strip(),
            "company_name": j.get("companyName") or "", "company_one_liner": j.get("companyOneLiner") or ""}


def company_jobs_quick(slug):
    # Cheaper than founders(): no website scrape, just the job count + titles for the new-role watcher.
    page = get(f"https://www.ycombinator.com/companies/{slug}")
    m = re.search(r'data-page="([^"]*)"', page or "")
    if not m:
        return {"slug": slug, "error": "Couldn't load this company's YC page"}
    props = json.loads(html.unescape(m.group(1)))["props"]
    jobs = open_jobs(props)
    return {"slug": slug, "job_count": len(jobs), "jobs": [j["title"] for j in jobs]}


def digest(known_batches_json, slug_list, known_jobs_json):
    try:
        known_batches = json.loads(known_batches_json) if known_batches_json else {}
        known_jobs = json.loads(known_jobs_json) if known_jobs_json else {}
    except ValueError:
        known_batches, known_jobs = {}, {}
    current = batches()
    new_batches = [b for b in current if known_batches.get(b["batch"]) != b["count"]]
    changed_jobs = []
    if slug_list:
        with cf.ThreadPoolExecutor(len(slug_list)) as ex:
            for r in ex.map(company_jobs_quick, slug_list):
                if not r.get("error") and r["job_count"] != known_jobs.get(r["slug"], 0):
                    changed_jobs.append(r)
    return {"checked_at": int(time.time()), "new_batches": new_batches, "changed_jobs": changed_jobs}


def route(query):
    q = urllib.parse.parse_qs(query)
    action = (q.get("action") or [""])[0]
    if action == "batches":
        return 200, batches(), 86400
    if action == "companies":
        batch_list = [b for b in (q.get("batch") or [""])[0].split(",") if b]
        if not batch_list or len(batch_list) > MAX_BATCHES or not all(BATCH_RE.match(b) for b in batch_list):
            return 400, {"error": f"Pass 1-{MAX_BATCHES} valid batches"}, 0
        return 200, companies(batch_list), 3600
    if action == "founders":
        slugs = [s for s in (q.get("slugs") or [""])[0].split(",") if s]
        if not slugs or len(slugs) > MAX_SLUGS or not all(SLUG_RE.match(s) for s in slugs):
            return 400, {"error": f"Pass 1-{MAX_SLUGS} valid slugs"}, 0
        with cf.ThreadPoolExecutor(len(slugs)) as ex:
            return 200, list(ex.map(founders, slugs)), 86400
    if action == "job":
        url = (q.get("url") or [""])[0]
        if not JOB_URL_RE.match(url):
            return 400, {"error": "Invalid job URL"}, 0
        result = job_detail(url)
        return (502 if "error" in result and len(result) == 1 else 200), result, 86400
    if action == "digest":
        slugs = [s for s in (q.get("slugs") or [""])[0].split(",") if s][:MAX_DIGEST_SLUGS]
        if not all(SLUG_RE.match(s) for s in slugs):
            return 400, {"error": "Invalid slug in digest request"}, 0
        known_batches = (q.get("known_batches") or [""])[0]
        known_jobs = (q.get("known_jobs") or [""])[0]
        return 200, digest(known_batches, slugs, known_jobs), 0
    return 400, {"error": "Unknown action"}, 0


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status, body, cache = route(urllib.parse.urlparse(self.path).query)
        except Exception as e:
            status, body, cache = 502, {"error": str(e) or "Upstream error"}, 0
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cache:
            self.send_header("Cache-Control", f"public, s-maxage={cache}, stale-while-revalidate=86400")
        self.end_headers()
        self.wfile.write(data)
