"""Daily report: data model + markdown and HTML rendering.

The report is the product. It is structured exactly as the spec requires:
  - one section per match (kickoff in Central, canonical venue, de-vigged book
    1X2 + total, Market 1X2, a one-line team-news availability note),
  - a ranked table of flagged edges with a factual templated rationale,
  - a correlation note when several flagged edges are the same directional bet,
  - an explicit "thin board / no qualifying edges" message when nothing clears,
  - a footer disclaimer (decision support only, fair values depend on the xG
    model, human judgment required, no bets placed automatically).

Rendering is pure: it formats whatever the pipeline put in :class:`DailyReport`
and never recomputes or fabricates anything.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

from .models import (
    Confidence,
    MatchLambdas,
    Match,
    ReferenceLines,
    XgStrategy,
)
from .screening import MatchScreen, ScreenStatus, _selection_label, ranking_formula

DISCLAIMER = (
    "Decision support only. Fair values come from the Poisson model anchored by "
    "the '{strategy}' xG strategy and are only as good as their inputs. Human "
    "judgment is required before placing any bet. This tool screens and ranks; "
    "it NEVER places, sizes, or executes a trade."
)


class MatchReport(BaseModel):
    """Everything the renderer needs for one match's section."""

    match: Match
    reference: ReferenceLines = Field(default_factory=ReferenceLines)
    home_news_known: bool = False
    away_news_known: bool = False
    lambdas: Optional[MatchLambdas] = None
    screen: MatchScreen = Field(default_factory=MatchScreen)
    # Market Yes prices (cents) for the 1X2 legs, when present.
    market_1x2: dict[str, Optional[int]] = Field(default_factory=dict)
    unmapped_count: int = 0
    note: str = ""


class DailyReport(BaseModel):
    report_date: date
    strategy: XgStrategy
    threshold_cents: int
    generated_at: datetime
    matches: list[MatchReport] = Field(default_factory=list)
    venue: str = "Polymarket"  # the exchange whose prices we screened against

    @property
    def total_flagged(self) -> int:
        return sum(len(m.screen.flagged) for m in self.matches)

    @property
    def liquidity(self) -> tuple[int, int]:
        """(markets with a Market price, total markets) across all matches — the
        liquidity gauge. Edges are impossible until the first number is > 0."""
        priced = total = 0
        for m in self.matches:
            for sm in (*m.screen.flagged, *m.screen.other):
                total += 1
                if sm.market_price_cents is not None:
                    priced += 1
        return priced, total

    def liquidity_line(self) -> str:
        priced, total = self.liquidity
        if total == 0:
            return f"{self.venue} liquidity: no markets discovered."
        if priced == 0:
            return f"{self.venue} liquidity: 0/{total} markets priced — no tradeable prices yet, so no edges are possible."
        return f"{self.venue} liquidity: {priced}/{total} markets priced."


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _pct(p: Optional[float]) -> str:
    return "—" if p is None else f"{round(p * 100)}%"


def _cents(c: Optional[int]) -> str:
    return "—" if c is None else f"{c}¢"


def _news_note(m: MatchReport) -> str:
    if m.home_news_known and m.away_news_known:
        return "team news: available for both sides"
    if m.home_news_known or m.away_news_known:
        return "team news: partial (one side unknown)"
    return "team news: unavailable (treated as unknown)"


def _book_1x2(ref: ReferenceLines) -> str:
    if ref.moneyline is None:
        return "book 1X2: unavailable"
    ml = ref.moneyline
    return f"book 1X2: H {_pct(ml.home)} / D {_pct(ml.draw)} / A {_pct(ml.away)}"


def _book_total(ref: ReferenceLines) -> str:
    if ref.total_line is None:
        return "book total: unavailable"
    return f"book total: {ref.total_line:g} (over {_pct(ref.over_prob)}, {ref.num_books} book(s))"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def render_markdown(report: DailyReport) -> str:
    L: list[str] = []
    L.append(f"# World Cup 2026 {report.venue} Edge Screener — {report.report_date.isoformat()}")
    L.append("")
    L.append(
        f"_xG strategy: **{report.strategy.value}** · threshold: **{report.threshold_cents}¢** · "
        f"generated {report.generated_at.strftime('%Y-%m-%d %H:%M %Z')}_"
    )
    L.append("")
    L.append(f"**{report.liquidity_line()}**")
    L.append("")

    if not report.matches:
        L.append("_No World Cup matches discovered for this date._")
        L.append("")
    elif report.total_flagged == 0:
        L.append(
            "> **Thin board — no qualifying edges today.** Nothing cleared the "
            f"{report.threshold_cents}¢ threshold across "
            f"{len(report.matches)} match(es). Passing is a valid outcome."
        )
        L.append("")

    for m in report.matches:
        L.extend(_markdown_match(m, report.threshold_cents))

    L.append("---")
    L.append("")
    L.append(f"_Ranking: {ranking_formula()}._")
    L.append("")
    L.append(f"_{DISCLAIMER.format(strategy=report.strategy.value)}_")
    return "\n".join(L)


def _markdown_match(m: MatchReport, threshold: int) -> list[str]:
    match = m.match
    venue = match.venue_canonical or "venue TBD"
    kickoff = match.kickoff_central().strftime("%a %b %d, %I:%M %p %Z")

    L: list[str] = []
    L.append(f"## {match.home.name} vs {match.away.name}")
    L.append(f"_{kickoff} · {venue}_")
    L.append("")
    L.append(f"- {_book_1x2(m.reference)}")
    L.append(f"- {_book_total(m.reference)}")
    k = m.market_1x2
    L.append(
        f"- Market 1X2: H {_cents(k.get('home'))} / D {_cents(k.get('draw'))} / "
        f"A {_cents(k.get('away'))}"
    )
    if m.lambdas is not None:
        L.append(
            f"- model λ: home {m.lambdas.lambda_home:.2f} / away {m.lambdas.lambda_away:.2f}"
        )
    L.append(f"- {_news_note(m)}")
    if m.unmapped_count:
        L.append(f"- _{m.unmapped_count} market(s) could not be mapped and were skipped_")
    L.append("")

    if not m.lambdas:
        L.append("_No reference lines → match unpriceable._")
        L.append("")
        L.extend(_markdown_suppressed(m))
        return L

    # Always show the model's fair-value sheet — useful as a reference even when
    # Market has no price yet (the "Market" / "Gap" columns just show —).
    L.extend(_markdown_sheet(m))

    flagged = m.screen.flagged
    if flagged:
        L.append(f"### Flagged edges ({len(flagged)})")
        L.append("")
        L.append("| Market | Side | Price | Fair | Gap | Conf | Rationale |")
        L.append("|---|---|---|---|---|---|---|")
        for e in flagged:
            L.append(
                f"| {_edge_label(e)} | {e.side.value.upper()} | {_cents(e.market_price_cents)} | "
                f"{_cents(e.fair_price_cents)} | {e.gap_cents}¢ | "
                f"{(e.confidence or Confidence.LOW).value} | {e.rationale} |"
            )
        L.append("")
        for g in m.screen.correlation_groups:
            L.append(f"> ⚠️ **Correlated ({g.direction.replace('_', ' ')}):** {g.note}")
            L.append("")
    else:
        L.append("_No flagged edges (Market prices missing, or all within threshold)._")
        L.append("")

    L.extend(_markdown_suppressed(m))
    return L


# Headline markets for the fair-value sheet (correct scores are a long tail and
# omitted here — they're in the persisted report.json).
_SHEET_KINDS = ("match_result", "advance", "over_under", "team_total", "btts")
_KIND_ORDER = {"match_result": 0, "advance": 1, "over_under": 2, "team_total": 3, "btts": 4}


def _sheet_markets(m: MatchReport):
    rows = [
        sm
        for sm in (*m.screen.flagged, *m.screen.other)
        if sm.fair_price_cents is not None and sm.fair_value.selection.kind in _SHEET_KINDS
    ]

    def key(sm):
        s = sm.fair_value.selection
        return (_KIND_ORDER.get(s.kind, 9), getattr(s, "team", "") or "", getattr(s, "line", 0) or 0)

    return sorted(rows, key=key)


def _markdown_sheet(m: MatchReport) -> list[str]:
    rows = _sheet_markets(m)
    if not rows:
        return []
    L = ["**Model fair values** (headline markets):", ""]
    L.append("| Market | Fair | Price | Gap |")
    L.append("|---|---|---|---|")
    for sm in rows:
        fair = sm.fair_price_cents
        kal = sm.market_price_cents
        gap = f"{abs(fair - kal)}¢" if kal is not None else "—"
        L.append(f"| {_selection_label(sm.fair_value)} | {fair}¢ | {_cents(kal)} | {gap} |")
    L.append("")
    return L


def _suppressed(m: MatchReport):
    return [s for s in m.screen.other if s.status == ScreenStatus.SUPPRESSED]


def _markdown_suppressed(m: MatchReport) -> list[str]:
    """Surface suppressed props with their note — shown, never silently dropped."""
    sup = _suppressed(m)
    if not sup:
        return []
    L = ["**Suppressed (not real edges):**", ""]
    for s in sup:
        L.append(f"- {s.note}")
    L.append("")
    return L


def _edge_label(e) -> str:
    # The rationale already embeds the market label; derive a short label here.
    return e.rationale.split(" at ")[0].replace("Market ", "") if e.rationale else "market"


# --------------------------------------------------------------------------- #
# HTML (email body)
# --------------------------------------------------------------------------- #
def render_html(report: DailyReport) -> str:
    P: list[str] = []
    P.append("<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px\">")
    P.append(
        f"<h1>World Cup 2026 {report.venue} Edge Screener — {report.report_date.isoformat()}</h1>"
    )
    P.append(
        f"<p style='color:#555'>xG strategy: <b>{report.strategy.value}</b> · "
        f"threshold: <b>{report.threshold_cents}¢</b> · "
        f"generated {report.generated_at.strftime('%Y-%m-%d %H:%M %Z')}</p>"
    )
    P.append(f"<p><b>{report.liquidity_line()}</b></p>")

    if not report.matches:
        P.append("<p><i>No World Cup matches discovered for this date.</i></p>")
    elif report.total_flagged == 0:
        P.append(
            "<p style='padding:10px;background:#fff3cd;border-radius:6px'>"
            f"<b>Thin board — no qualifying edges today.</b> Nothing cleared the "
            f"{report.threshold_cents}¢ threshold across {len(report.matches)} match(es). "
            "Passing is a valid outcome.</p>"
        )

    for m in report.matches:
        P.append(_html_match(m))

    P.append("<hr>")
    P.append(f"<p style='color:#777;font-size:13px'>Ranking: {ranking_formula()}.</p>")
    P.append(
        f"<p style='color:#777;font-size:13px'>{DISCLAIMER.format(strategy=report.strategy.value)}</p>"
    )
    P.append("</div>")
    return "\n".join(P)


def _html_match(m: MatchReport) -> str:
    match = m.match
    venue = match.venue_canonical or "venue TBD"
    kickoff = match.kickoff_central().strftime("%a %b %d, %I:%M %p %Z")
    k = m.market_1x2

    rows: list[str] = []
    rows.append(f"<h2>{match.home.name} vs {match.away.name}</h2>")
    rows.append(f"<p style='color:#555'>{kickoff} · {venue}</p>")
    rows.append("<ul>")
    rows.append(f"<li>{_book_1x2(m.reference)}</li>")
    rows.append(f"<li>{_book_total(m.reference)}</li>")
    rows.append(
        f"<li>Market 1X2: H {_cents(k.get('home'))} / D {_cents(k.get('draw'))} / "
        f"A {_cents(k.get('away'))}</li>"
    )
    if m.lambdas is not None:
        rows.append(
            f"<li>model λ: home {m.lambdas.lambda_home:.2f} / away {m.lambdas.lambda_away:.2f}</li>"
        )
    rows.append(f"<li>{_news_note(m)}</li>")
    rows.append("</ul>")

    if not m.lambdas:
        rows.append("<p><i>No reference lines → match unpriceable.</i></p>")
        rows.append(_html_suppressed(m))
        return "\n".join(rows)

    # Always-present fair-value sheet.
    sheet = _sheet_markets(m)
    if sheet:
        rows.append("<p><b>Model fair values</b> (headline markets):</p>")
        rows.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>")
        rows.append("<tr style='background:#f0f0f0'><th>Market</th><th>Fair</th><th>Price</th><th>Gap</th></tr>")
        for sm in sheet:
            kal = sm.market_price_cents
            gap = f"{abs(sm.fair_price_cents - kal)}¢" if kal is not None else "—"
            rows.append(
                f"<tr><td>{_selection_label(sm.fair_value)}</td><td>{sm.fair_price_cents}¢</td>"
                f"<td>{_cents(kal)}</td><td>{gap}</td></tr>"
            )
        rows.append("</table>")

    if m.screen.flagged:
        rows.append("<p><b>Flagged edges</b></p>")
        rows.append("<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>")
        rows.append(
            "<tr style='background:#f0f0f0'><th>Market</th><th>Side</th><th>Price</th>"
            "<th>Fair</th><th>Gap</th><th>Conf</th><th>Rationale</th></tr>"
        )
        for e in m.screen.flagged:
            rows.append(
                f"<tr><td>{_edge_label(e)}</td><td>{e.side.value.upper()}</td>"
                f"<td>{_cents(e.market_price_cents)}</td><td>{_cents(e.fair_price_cents)}</td>"
                f"<td>{e.gap_cents}¢</td><td>{(e.confidence or Confidence.LOW).value}</td>"
                f"<td>{e.rationale}</td></tr>"
            )
        rows.append("</table>")
        for g in m.screen.correlation_groups:
            rows.append(
                "<p style='padding:8px;background:#fff3cd;border-radius:6px'>"
                f"⚠️ <b>Correlated ({g.direction.replace('_', ' ')}):</b> {g.note}</p>"
            )
    else:
        rows.append("<p><i>No flagged edges (Market prices missing, or all within threshold).</i></p>")

    rows.append(_html_suppressed(m))
    return "\n".join(rows)


def _html_suppressed(m: MatchReport) -> str:
    sup = _suppressed(m)
    if not sup:
        return ""
    items = "".join(f"<li>{s.note}</li>" for s in sup)
    return f"<p><b>Suppressed (not real edges):</b></p><ul>{items}</ul>"
