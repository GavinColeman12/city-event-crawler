# City Event Crawler

Discover fun events across European cities by vibe, ranked by popularity. Multi-agent pipeline that aggregates events from 13+ sources (Google, Instagram, Resident Advisor, Dice.fm, Meetup, FetLife, Event Guides, and more).

## What it does

Pick a city and date, and the crawler finds events from:

- **Google Events** - via SearchAPI.io
- **Instagram** - auto-discovers club/promoter accounts via Google, scrapes posts via Apify
- **Resident Advisor** - techno/house/electronic music events
- **Dice.fm** - live music and club nights
- **Meetup** - social groups, expat communities, language exchanges
- **FetLife** - public kink/fetish events (munches, play parties, workshops)
- **Event Guides** - Songkick, Bandsintown, TimeOut, AllEvents, Xceed, Festicket, Skirt Club, Them.us, Eater, ClassPass, GoOut, and more
- **Blog Scraper** - local event listing sites (WeLoveBudapest, ExBerliner, etc.)

Plus optional sources with their own API keys:
- Eventbrite, Ticketmaster, Reddit, X/Twitter, Facebook

## Multi-agent architecture

```
Researcher -> Crawler -> Classifier -> Quality -> Ranker
```

1. **Researcher Agent** - Auto-discovers city-specific sources (Instagram accounts, subreddits, local sites)
2. **Crawler Agent** - Dispatches all platform crawlers in parallel
3. **Classifier Agent** - Categorizes events by vibe using keyword + venue + source signals
4. **Quality Agent** - Deduplicates across platforms, validates data
5. **Ranker Agent** - Sorts by engagement-weighted popularity score

## 15 vibe categories

Social, Dating, Kinky, Nightlife, Music, Art & Culture, Food & Drink, Wellness, Adventure, Networking, LGBTQ+, Underground, Festival, Sport & Fitness, Other

## 45+ European cities

Budapest, Berlin, Prague, Barcelona, Amsterdam, Lisbon, Vienna, Warsaw, Krakow, Belgrade, London, Paris, Rome, Milan, Athens, Dublin, Copenhagen, Stockholm, Oslo, Helsinki, Madrid, Munich, Istanbul, and more.

## Running it

### Prerequisites

- Python 3.10+
- Node 18+
- [SearchAPI.io key](https://www.searchapi.io/) (required - $20/mo for 10k searches)
- [Apify token](https://apify.com/) (optional but recommended for Instagram - $5 free credits)

### Backend

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
# Edit .env with your API keys
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000

### Docker

```bash
docker compose up --build
```

## API keys

All keys go in `.env` (copy from `.env.example`):

| Key | Cost | Notes |
|-----|------|-------|
| `SERPAPI_KEY` | $20/mo | SearchAPI.io (powers 8 of the 13 crawlers) |
| `INSTAGRAM_APIFY_TOKEN` | $5 free / $49/mo | Apify Instagram scraping |
| `EVENTBRITE_TOKEN` | Free | Optional |
| `TICKETMASTER_API_KEY` | Free | Optional |
| `REDDIT_CLIENT_ID` + `SECRET` | Free | Optional |
| `TWITTER_BEARER_TOKEN` | $100/mo | Optional |
| `FACEBOOK_ACCESS_TOKEN` | Free (needs review) | Optional |

The blog scraper, RA GraphQL API, and SearchAPI-powered crawlers all work without any additional keys once SearchAPI is configured.

## Project structure

```
.
├── backend/
│   ├── main.py                # FastAPI app + /api/search endpoint
│   ├── config.py              # Settings + city coordinates
│   ├── models.py              # Event, SearchRequest, SearchResponse
│   ├── agents/                # Multi-agent orchestration
│   │   ├── researcher.py      # Discovers accounts/venues per city
│   │   ├── crawler_agent.py   # Parallel crawler dispatcher
│   │   ├── classifier_agent.py
│   │   ├── quality_agent.py
│   │   ├── ranker_agent.py
│   │   └── orchestrator.py
│   ├── crawlers/              # Platform-specific crawlers
│   │   ├── google_events.py
│   │   ├── instagram.py
│   │   ├── resident_advisor.py
│   │   ├── dice.py
│   │   ├── meetup.py
│   │   ├── fetlife.py
│   │   ├── event_guides.py    # Songkick, TimeOut, AllEvents, etc.
│   │   ├── blog_scraper.py
│   │   ├── eventbrite.py
│   │   ├── ticketmaster.py
│   │   ├── reddit.py
│   │   ├── twitter.py
│   │   └── facebook.py
│   └── services/              # Aggregation, geocoding, classification
├── frontend/                  # React + Vite + Leaflet
│   └── src/
│       ├── App.jsx
│       └── components/        # SearchBar, EventCard, FilterPanel, MapView
├── docker-compose.yml
└── .env.example
```

## API endpoints

- `POST /api/search` - search events (body: `{city, date, vibes?, platforms?, radius_km?}`)
- `GET /api/cities` - list supported cities
- `GET /api/vibes` - list vibe categories
- `GET /api/sources` - list event sources
- `GET /api/health` - health check

## License

Private project.
