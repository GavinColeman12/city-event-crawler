"""City digest — a lean "what's on today + tomorrow" job, independent of the NYC pipeline.

Unlike ``backend/pipeline.py`` this package needs no database, no Apify and no
Anthropic key.  Collectors pull structured events (with each source's own
popularity numbers) over plain HTTP; the final ranking is done either by the
scheduled Claude routine that drives the job or by the built-in heuristic
(``--auto``).  The only third-party dependency is ``httpx``.

Entry point: ``python -m backend.digest --help``.  See ``backend/digest/README.md``.
"""
