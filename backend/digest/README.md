# City digest

A daily "what's on today + tomorrow" message for one city, ranked for the
international / expat crowd and delivered to WhatsApp. Built for Barcelona.

It is independent of the NYC search pipeline in `backend/pipeline.py`: no
database, no Anthropic key, and nothing to install beyond `httpx`.

## How it works

```
collect  →  pick  →  render  →  send
```

| Step | What happens | Who does it |
|------|--------------|-------------|
| collect | Scrape every source, drop kink / fetish / swinger listings, merge duplicates, bucket into today / tomorrow, shortlist | `python -m backend.digest collect` |
| pick | Choose up to 5 per day and write one "why" line each | the scheduled Claude task (see `RUNBOOK.md`), or the heuristic in `auto` mode |
| render | Build the WhatsApp text. Every fact (time, venue, price, headcount, link) comes from the collected data, never from the picker | `python -m backend.digest render` |
| send | Deliver via the configured WhatsApp gateway | `python -m backend.digest send` |

"Today" runs 06:00 → 06:00, so a set that starts at 01:00 belongs to the night before.

## Sources

| Source | Module | Popularity numbers it publishes |
|--------|--------|---------------------------------|
| Meetup | `collectors/meetup.py` | going, RSVP limit, waitlist, group members |
| Resident Advisor | `collectors/ra.py` | interested, RA Pick |
| DICE | `collectors/dice.py` | none (sold-out / waiting-list state only) |
| Xceed | `collectors/xceed.py` | Xceed popularity rank, venue followers |
| Eventbrite | `collectors/eventbrite.py` | organiser followers, "going fast" / "few tickets left" |
| Luma | `collectors/luma.py` | guests, spots remaining |
| Instagram (optional) | `collectors/instagram.py` | likes / comments per post, plus each account's live stories (images saved next to the candidates file) — needs `INSTAGRAM_APIFY_TOKEN` and a working Apify account |

All of these are the sites' own public endpoints, called without login. They are
undocumented and will change; each collector raises `CollectorError` when its
source is unreachable and the digest carries on without it. A collector that
could only fetch part of its data says so with `logger.warning`; the engine
captures those lines, and the source is marked "(partial)" in the message footer.

## Commands

```bash
python -m backend.digest check                                   # configuration + which hosts are reachable
python -m backend.digest collect --out c.json --brief brief.txt  # scrape + shortlist
python -m backend.digest render --candidates c.json --picks picks.json --out message.txt
python -m backend.digest send --file message.txt [--dry-run]
python -m backend.digest auto [--send]                           # everything, heuristic picks, no model
python -m backend.digest.collectors.meetup                       # live smoke for one source
```

Exit codes: `0` ok · `1` bad input · `2` nothing collected · `3` WhatsApp not configured · `4` send failed.

## Configuration

Environment variables (locally they may also live in `.env`; never commit them):

| Variable | Meaning |
|----------|---------|
| `WHATSAPP_PROVIDER` | `callmebot` (default) or `textmebot` |
| `WHATSAPP_PHONE` | your number in international format, e.g. `+34600000000` |
| `WHATSAPP_APIKEY` | the key the provider gave you |
| `WHATSAPP_CHUNK_CHARS` | optional — longest single message (default 2000; a full digest goes out as two) |
| `INSTAGRAM_APIFY_TOKEN` | optional — turns the Instagram source on |

Both WhatsApp providers take the number and key as URL parameters (that is their
API), so the provider sees your number and the message text. The code never logs
a request URL.

## The scheduled task

The 3 pm run is a Claude Code scheduled task (claude.ai/code/routines). Its prompt
checks out this code and follows `RUNBOOK.md`. For it to work, the task's cloud
environment needs:

- **Network access → Allowed domains**: `www.meetup.com`, `ra.co`, `dice.fm`,
  `*.xceed.me`, `www.eventbrite.com`, `api.lu.ma`, `lu.ma`, `luma.com`,
  the WhatsApp provider's host (`api.callmebot.com` or `api.textmebot.com`), and
  `api.apify.com` if Instagram is on. Story images are downloaded from Instagram's
  image servers, so also allow `*.cdninstagram.com` and `*.fbcdn.net`.
- **Environment variables**: the `WHATSAPP_*` values above.

## Tests

```bash
env -u ANTHROPIC_API_KEY backend/.venv/bin/pytest tests/backend/test_digest_*.py -q
```

All offline: collectors are tested against payloads captured from the real sites,
delivery against a mock transport.

## Adding a city or a source

- **City**: add it to `CITIES` in `cities.py`, then give each collector its
  parameters in that module's `CITY_PARAMS` (RA area id, DICE city slug, …).
- **Source**: add `collectors/<name>.py` following the contract at the top of
  `collectors/__init__.py`, and list it in `REGISTRY`.
