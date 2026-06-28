# World Cup 2026 Kalshi Edge Screener

A scheduled **decision-support** tool. Every morning it finds that day's 2026
FIFA World Cup matches, pulls Kalshi market prices plus sharp-book reference
lines and team news, prices each market with a Poisson model, and emails a
ranked shortlist of markets where Kalshi diverges from fair value.

> **This is a screener, not an autotrader.** It never places, sizes, or executes
> a trade and never connects to order-placement endpoints. Output is a research
> sheet you review before betting manually. This is enforced in code — see
> [Guardrails](#guardrails).

---

## Read this in five minutes: what's trustworthy, what's a placeholder

| Component | Status | Trust |
|---|---|---|
| Poisson pricing engine (`pricing/poisson.py`) | **Built + tested** | High — checked against hand computation and analytic identities |
| `book_anchored` xG strategy (default) | **Built + tested** | Medium — anchors to the book's own goal expectation; only as good as the book |
| `form_blend` xG strategy | **STUB** | **Do not trust** — uncalibrated, no opponent adjustment (TODO) |
| Pricing engine wrapper / confidence / 1H scaling | **Built + tested** | High for plumbing; 1H fraction is an approximation (see below) |
| Guardrails (no-trade scan) | **Built + tested** | High |
| HTTP/cache foundation (`clients/http.py`, `cache.py`) | **Built + tested** | High |
| KalshiClient — read-only API access (`clients/kalshi.py`) | **Built + tested** | High |
| Kalshi market → selection mapping (`kalshi_markets.py`) | **Built + tested** | **Provisional** — heuristic; re-validate against real captured market data |
| OddsClient + multiplicative de-vigging (`clients/odds.py`) | **Built + tested** | High for the de-vig math; **fixtures synthetic** — validate response shape against a real Odds API capture |
| NewsClient (`clients/news.py`) | **Built + tested** | **Stubbed to "unknown"** — `StubNewsClient` is the wired default; the HTTP provider is an unvalidated skeleton |
| Screening / ranking / correlation / prop suppression (`screening.py`) | **Built + tested** | High for the logic; prop suppression is live but inert until a real news provider feeds it (news is stubbed) |
| Report rendering — markdown + HTML (`report.py`) | **Built + tested** | High |
| End-to-end pipeline wiring (`pipeline.py`, `run.py`) | **Built + tested** | High for orchestration; match discovery/venue still lean on provisional Kalshi/odds mapping |
| Shadow-mode persistence + grading (`storage.py`, `settle.py`, `grade.py`) | **Built + tested** | High; results are manual-CSV input for now |
| AWS runtime (`aws/`: S3 store, SES, Secrets, Lambda handler) | **Built + tested** | High — logic tested with fakes; real AWS calls not exercised in CI |
| CDK infrastructure (`infra/`) | **Built** | **Unverified** — written conventionally but not synthesized/deployed here; needs your AWS account + Docker |

**Current state: deliverables 1–9 complete** (CDK infra written but not yet
deployed — see below). The full local pipeline runs end-to-end (discover →
Kalshi → odds → news → price → screen → report), persists each run, grades for
PnL + calibration in shadow mode, and the same pipeline runs in Lambda writing
to S3 and emailing via SES. Run it offline with `--demo` (no network, no keys),
or against the live APIs with `--date`.

### Fastest way to start testing (local, no AWS)
The deploy automates the 9am run; it is **not** the quick test loop. To test the
screener's value now: get a free [The Odds API](https://the-odds-api.com) key, then
```bash
export SCREENER_ODDS_API_KEY=your-key
uv run python -m screener.run --date 2026-06-21   # live Kalshi (public) + Odds
```
Review `output/date=.../report.md`, then grade it once results are in (see
[Shadow mode](#shadow-mode-do-this-before-trusting-any-output)). The provisional
bits (Kalshi market mapping, odds outcome-naming) are what to validate first
against the real responses now cached under `.cache/`.

> **News is currently stubbed to "unknown".** Until a real news provider is
> validated and wired, confidence is downgraded wherever team news matters and
> **no injury-driven player-prop suppression occurs** — props are still excluded
> from goal-model pricing, but the screener cannot yet detect that a flagged prop
> is injury-driven. Keep this in mind when reviewing prop-adjacent output.

---

## Quick start

```bash
uv sync --extra dev          # create venv, install deps
uv run pytest                # run the full test suite
uv run python -m screener.run --demo   # run the WHOLE pipeline offline, no network
```

`--demo` runs the entire pipeline against synthetic in-memory clients and prints
a real report — seeded to show flagged edges, a correlation group, and an
injury-suppressed player prop. It writes `reports/<date>.md` and `.html`.

The real daily run hits the live APIs:

```bash
uv run python -m screener.run --date 2026-06-21
```

Kalshi market data is public; set `SCREENER_ODDS_API_KEY` for reference lines
(absent → lines degrade to missing). News is stubbed to "unknown" until a real
provider is wired.

---

## The Poisson model: where it's right and where it's wrong

The engine takes two expected-goals numbers (`lambda_home`, `lambda_away`) and
derives every game-level market by summing the joint scoreline distribution.

**Assumptions, all documented inline where they're used:**

1. **Independence** (`poisson.py`): home and away goals are independent Poissons.
   This slightly **understates draws and BTTS-No**. A Dixon-Coles low-score
   correction would fix it; deliberately omitted to keep the core simple and
   hand-checkable.
2. **xG split** (`xg.py`, `book_anchored`): total goal expectation is backed out
   of the book total line (inverting the de-vigged over probability when
   available, else using the line as a mean — a documented approximation), then
   split between teams by fitting the model's home-win probability to the
   de-vigged moneyline. The favorite naturally gets the larger lambda.
3. **First-half fraction** (`engine.py`): 1H lambda ≈ `0.45 ×` full-game lambda
   (configurable). An approximation — a real model would estimate a separate 1H
   rate. 1H markets are confidence-downgraded one notch as a result.

**Knockout "to advance" markets** (`KXWCADVANCE`, 2-way, no draw) are priced by
composing the 90-minute result with extra time + a penalty shootout:
`P(advance) = P(win 90) + P(draw 90) × P(win the ET/pens)`. Extra time is modeled
as Poisson over `extra_time_fraction` of a game and the shootout split by
`penalty_split_home` (0.5 = coin flip) — both approximations, so advance markets
are confidence-downgraded a notch. The regulation-time result market (`KXWCGAME`)
still keeps its draw — penalties only decide advancement, not the 90' score.

**Not modeled:** corners (need their own model — carried through unpriced and
excluded from screening).

Every `FairValue` carries its probability, the lambdas + strategy used, and a
confidence tag (`high`/`medium`/`low`) driven by **input quality** (number of
reference books, whether team news is known, approximate market types), never by
the size of the edge.

---

## Kalshi API (read-only)

Base URL (production): `https://api.elections.kalshi.com/trade-api/v2`
(demo: `https://demo-api.kalshi.co/trade-api/v2`). **Market-data endpoints are
public — no authentication required.** If Kalshi ever gates them, an API key can
be passed as a header, but **no order/portfolio scope is ever requested**.

Endpoints used (all `GET`, all read-only):

| Endpoint | Purpose | Key params |
|---|---|---|
| `/events` | list events | `series_ticker`, `status`, `limit`, `cursor` |
| `/events/{event_ticker}` | one event + nested markets | `with_nested_markets` |
| `/markets` | list markets | `event_ticker`, `status`, `series_ticker`, `tickers`, `limit`, `cursor` |
| `/markets/{ticker}` | one market | — |
| `/markets/{ticker}/orderbook` | market orderbook (read) | — |

List endpoints are cursor-paginated (page size 100); the client follows the
cursor with a hard page cap. Prices are integer cents; we screen against the
Yes price, taken as the bid/ask midpoint when both are quoted, else last trade.

**Market → selection mapping is heuristic and provisional** (`kalshi_markets.py`).
The exact ticker/title grammar of Kalshi World Cup markets has not yet been
captured from the live API, so the patterns are a best effort against the
documented schema and **must be re-validated against recorded real responses**.
Unmapped markets are returned separately, never silently dropped, so report
coverage is visible. Fixtures in `tests/fixtures/` are synthetic placeholders to
be replaced with real captures.

## The Odds API (read-only)

Base URL: `https://api.the-odds-api.com/v4`. One endpoint:

| Endpoint | Purpose | Key params |
|---|---|---|
| `/sports/{sport}/odds` | per-event odds across books | `apiKey`, `regions`, `markets`, `oddsFormat=decimal` |

World Cup sport key: `soccer_fifa_world_cup`. For soccer, `h2h` is **three-way**
(Home / Draw / Away); `totals` is Over/Under at a point line; `team_totals` is
requested opportunistically and degrades to empty when absent. The API key is a
**query param** read from `SCREENER_ODDS_API_KEY` — never hardcoded, never logged.

**De-vigging is multiplicative (proportional)**, per the spec: each book's raw
implied probs (`1/decimal_odds`) are normalized to sum to 1 (removing the margin
proportionally); across books we take the **median per outcome, then
re-normalize** (three independent medians need not sum to 1). The consensus total
line is the **mode across books, median as tiebreaker**. `num_books` (books
contributing a complete 1X2) feeds the confidence tag. The de-vig functions are
pure and unit-tested against hand-computed values. **Fixtures are synthetic** —
validate the exact outcome-naming and `team_totals` shape against a real capture.

## News (read-only, currently stubbed)

`NewsClient` is an interface with two implementations: `StubNewsClient` (the
wired default — always returns `TeamNews(known=False)`, never raises) and
`HttpNewsClient` (an API-Football-shaped skeleton, **not validated/wired**, that
maps every failure to `known=False`). `known=False` means "we couldn't find out",
distinct from "known and nobody is out" — a distinction the confidence tagging
and prop-suppression logic depend on. Until a real provider is wired, news is
always "unknown" (see the banner above).

## Player props (critical domain rule, encoded for screening)

Kalshi settles player props at the **last fair price before a player is ruled
out**, *not* to No. So a prop divergence driven by injury news is **not a real
edge** once the news is public. The pricing layer refuses to give props a
goal-model price; the screening stage (`screening.py`) **suppresses** a prop
(status `SUPPRESSED`, with an explanatory note) when team news says its player
is out or doubtful, matching the player name with a normalized/initial matcher.
Game-level markets (totals, team totals, BTTS, moneyline, spreads) settle
normally and are unaffected. **This is live but inert until a real news provider
is wired** — with news stubbed to "unknown", no prop is ever suppressed yet.

---

## Guardrails

Enforced in code, not just docs:

- `guardrails.assert_read_only()` scans the package source for any
  order/trade/portfolio-write identifier (`create_order`, `place_order`,
  `execute_trade`, …) and raises at startup if any appears. `run.py` calls it
  before doing anything; `tests/test_guardrails.py` calls it too, so a
  regression fails CI.
- The only outbound side effects the design permits are: **reading APIs, writing
  to S3, sending email.** Any other network write is a guardrail breach.

---

## Configuration

All via environment variables (prefix `SCREENER_`); no secrets in code. Locally
read from env, in Lambda from Secrets Manager / env.

| Var | Default | Meaning |
|---|---|---|
| `SCREENER_XG_STRATEGY` | `book_anchored` | `book_anchored` or `form_blend` (stub) |
| `SCREENER_FIRST_HALF_FRACTION` | `0.45` | 1H lambda as a fraction of full game |
| `SCREENER_EXTRA_TIME_FRACTION` | `0.333` | extra-time goals as a fraction of a 90-min game (knockout "to advance" pricing) |
| `SCREENER_PENALTY_SPLIT_HOME` | `0.5` | home share of a penalty shootout (0.5 = coin flip) |
| `SCREENER_MAX_GOALS` | `15` | scoreline grid truncation |
| `SCREENER_THRESHOLD_CENTS` | `3` | divergence flag threshold |
| `SCREENER_TIMEZONE` | `America/Chicago` | display timezone |
| `SCREENER_LOG_JSON` | `false` | JSON logs (set true in Lambda) |
| `SCREENER_ODDS_API_KEY` | _(none)_ | The Odds API key (query param); Secrets Manager in Lambda |
| `SCREENER_NEWS_API_KEY` | _(none)_ | news provider key; when unset, news degrades to "unknown" |
| `SCREENER_OUTPUT_DIR` | `output` | where reports + grades are persisted (date-partitioned; S3 in Lambda) |

---

## Shadow mode (DO THIS BEFORE TRUSTING ANY OUTPUT)

Shadow mode = **run the screener with no real money, save everything, then grade
it against actual results** to check whether the model is any good *before* you
ever bet on it. It is not optional.

Every run persists its full output, date-partitioned, under `output/date=<date>/`
(`report.json` + `report.md` + `report.html`). `report.json` is the complete
record — inputs, lambdas, every fair value (flagged and not), and the flagged
edges — so it can be graded later.

After the matches finish, write a results CSV and grade the saved run:

```bash
# results.csv — half-time columns optional (needed only for first-half markets)
# match_id,home_score,away_score,ht_home,ht_away
# KXWC2026-USAMEX,1,0,0,0

uv run python -m screener.grade --date 2026-06-21 --results results.csv
```

Grading produces two things (and saves `grade.md` / `grade.json` alongside):

- **PnL** — each *flagged* edge graded win/loss, with hypothetical profit at 1
  Kalshi contract (pays 100¢ if your backed side resolves, you paid the entry
  price of that side). This is "what if I'd bet it", never a real wager.
- **Calibration** — across *all* priced markets, did the ones the model called
  ~55% actually resolve true ~55% of the time? If predicted vs. actual diverge
  consistently, the lambdas are off and the "edges" are noise.

**Run this for at least one full matchday slate and review calibration before
acting on any output.** A few profitable edges mean nothing if calibration is
bad — that's variance, not skill.

**Convenience + aggregate.** `bash grade_day.sh <date>` downloads that day's
report from S3 and writes a `results.csv` template (fill scores, re-run to
grade). Once you've graded several days, `bash grade_day.sh all` (or
`python -m screener.grade --all`) aggregates them into one report with a **Brier
score** (single-number model accuracy: 0 = perfect, 0.25 = always guessing 50%)
and combined calibration buckets — the statistically meaningful view, since
per-day samples are small and noisy.

---

## Build order

1. ✅ Scaffold, config, models, logging, local entrypoint
2. ✅ Poisson pricing engine + full unit tests
3. ✅ KalshiClient (read-only) + market discovery, with fixtures (mapping provisional)
4. ✅ OddsClient with multiplicative de-vigging, unit-tested on sample book prices
5. ✅ NewsClient — interface + graceful "unknown" stub (default); HTTP provider is an unvalidated skeleton
6. ✅ Screening + confidence-weighted ranking + correlation grouping + prop suppression
7. ✅ Report rendering (markdown + HTML) and end-to-end pipeline wiring
8. ✅ Shadow-mode persistence (date-partitioned) + `screener.grade` (PnL + calibration)
9. ✅ AWS: Lambda handler + S3/SES/Secrets (tested with fakes) + CDK infra in `infra/` (deploy needs your AWS account + Docker; see `infra/README.md`)
