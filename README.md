# YC Outreach

Pick any Y Combinator batch, get every company's founders and their likely email addresses, and write a personalised
cold email to each one from a single template. Free by default (no API keys, no sign-up); optional verified emails
with your own Apify token.

```
python3 serve.py        # open http://localhost:8765
```

Python 3.9+, standard library only. Nothing to install.

## What it does

1. **Pick a batch** (Summer 2005 through the latest) from YC's public directory.
2. **Founders load 20 companies at a time** (about 5–10 s per 20); click **Load more** for the next 20. For each
   company the server reads its ycombinator.com page for the founders' names, titles, LinkedIn and X, and for how
   many roles that company has open on YC (title and link for each). Loaded companies are kept in your browser, so
   reopening a batch is instant. Check **hiring** to hide companies with no open roles, or pick an **industry**
   (B2B, Fintech, Healthcare, ...) from the dropdown to narrow the batch to the sector you actually want to reach.
3. **Emails are filled in** for each founder, best source first:

   | Label | Source | Reliability |
   |---|---|---|
   | `verified` | Apify lookup with your token (optional) | Checked by Apify |
   | `unverified` | Apify lookup that couldn't confirm the mailbox | Usually right |
   | `on site` | Email published on the company's homepage or `/contact` page | Real address, may be generic |
   | `guess` | `first@domain`, then `first.last@`, `flast@`, `firstlast@` | First guess right ~80% of the time |

   The reliability figure comes from a check against 151 Apify-verified founder emails: the first guess matched 80%, and
   the right address was somewhere in the guess list 93% of the time.
4. **Write once, send many.** Fill in your name and links, edit the subject and body, and every draft updates live.
   Copy the text or open it in your mail app. Mark companies as sent to hide them.

Your details, template, "sent" marks and loaded batches are stored in your browser (`localStorage`). There's no
database and no account.

## Deploy

Import the repo on [Vercel](https://vercel.com/new). No build step, no environment variables. `index.html` is served
as a static page and `api/yc.py` runs as a Python serverless function (`vercel.json` gives it 60 s).

## Project layout

| File | Role |
|---|---|
| `index.html` | The whole UI: one HTML file with inline CSS and JS, no framework, no build. |
| `api/yc.py` | Serverless function (Vercel Python runtime, `handler` class). YC search + founder pages. |
| `serve.py` | Local dev server: serves `index.html` and routes `/api/yc` to the same handler. |
| `vercel.json` | Function timeout. |
| `yc_scraper.py` | CLI: scrape whole batches to JSON/CSV. |
| `apify_enrich.py` | CLI: add Apify-found emails to the scraper's output. |

## HTTP API

All endpoints are `GET` and return JSON. Errors return `{"error": "..."}` with status 400 (bad input) or 502 (YC
unreachable).

### `/api/yc?action=batches`

```json
[{"batch": "Fall 2026", "count": 110}, {"batch": "Summer 2026", "count": 231}]
```

Newest first. Includes `"Unspecified"`, which the UI hides.

### `/api/yc?action=companies&batch=Winter%202024`

```json
[{"name": "Indemni", "slug": "indemni", "batch": "Winter 2024", "website": "http://www.indemni.com",
  "one_liner": "Cargo Theft and Fraud Prevention Platform", "industry": "B2B -> Supply Chain and Logistics",
  "team_size": 7, "launched_at": 1708029636}]
```

`batch` must look like `Winter 2024` (season + year).

### `/api/yc?action=founders&slugs=indemni,parcelbio`

At most 10 slugs per call (`^[a-z0-9-]+$`). The UI loads 20 companies per click as two parallel calls. About
5–9 s per call; each company's website check is cut off after 4 s (`SITE_DEADLINE`) so one slow site can't stall
the batch.

```json
[{"slug": "indemni", "website": "http://www.indemni.com", "domain": "indemni.com",
  "linkedin": "https://www.linkedin.com/company/...", "twitter": "", "site_emails": [],
  "job_count": 1, "jobs": [{"title": "Founding Engineer", "url": "https://www.ycombinator.com/companies/indemni/jobs/..."}],
  "founders": [{"name": "Omar Draz", "title": "Founder", "linkedin": "https://linkedin.com/in/odraz",
                "twitter": "https://twitter.com/oamdraz", "emails_found": [],
                "email_guesses": ["omar@indemni.com", "omar.draz@indemni.com", "odraz@indemni.com", "omardraz@indemni.com"]}]}]
```

A company whose YC page fails to load comes back as `{"slug": "...", "error": "..."}`. Guesses are empty when the
domain doesn't resolve.

## Where the data comes from

- **Batches and companies:** YC's public company search (Algolia). The read-only search key is read from
  `ycombinator.com/companies` at runtime, so no key is stored here.
- **Founders and open roles:** the `data-page` JSON embedded in each `ycombinator.com/companies/<slug>` page
  (`props.company` and `props.jobPostings`). Incomplete postings are skipped. Browsers can't fetch these
  cross-origin, which is why this part runs on a server.
- **On-site emails:** the homepage and `/contact` page, plain text and Cloudflare's "email protection"
  obfuscation (`data-cfemail="<hex>"`, shown on the page as `[email protected]`) decoded the same way browsers
  render it. About 1 in 16 company sites use this, so without decoding it those real, published addresses would
  silently fall back to a lower-reliability guess.
- **Verified emails (optional):** the Apify actor
  [`snipercoder/email-finder-by-name-and-domain`](https://apify.com/snipercoder/email-finder-by-name-and-domain),
  called **from the browser** with the visitor's own token, so the token never reaches the server. Results are
  matched on the `Name` and `Domain` fields of each dataset item. Each run covers up to 50 founders and is capped
  with `maxTotalChargeUsd`.

## Command line

For bulk exports. Output files are gitignored.

```
python3 yc_scraper.py --batches "Winter 2024" "Summer 2024" --out yc_founders   # -> yc_founders.json + .csv
python3 apify_enrich.py --input yc_founders.json --out yc_founders_enriched      # needs APIFY_TOKEN
```

`yc_scraper.py` flags: `--batches` (default `"Summer 2026" "Fall 2026"`), `--out`, `--workers` (default 12),
`--limit` (first N companies, for testing). It checks six contact pages per site and retries, so it's slower but
more thorough than the web API.

`apify_enrich.py` reads `APIFY_TOKEN` from the environment or a `.env` file (gitignored). Flags: `--input`, `--out`,
`--limit`, `--chunk` (founders per run, default 50), `--max-charge` (USD cap per run, default 1).

## Notes for AI agents and contributors

- Standard library only, on purpose. Don't add dependencies or a build step.
- `api/yc.py` and `yc_scraper.py` share logic but are separate on purpose: the function has to finish inside a
  serverless timeout, so it makes one attempt per fetch, checks 2 pages, and gives up on a website after 4 s;
  the CLI retries and checks 6.
- The function only accepts a batch name or slugs. Websites always come from YC's data, never from the request, so
  it can't be used to fetch arbitrary URLs. Keep it that way.
- Everything from YC is untrusted text. `index.html` escapes it (`esc()`) and only links `http(s)` URLs (`url()`).
- To test locally: `python3 serve.py`, then
  `curl 'localhost:8765/api/yc?action=founders&slugs=reddit'`.

## Be decent

Write to people one at a time, keep it short, and take "no" for an answer.

## License

MIT
