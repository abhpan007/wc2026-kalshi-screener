"""Shadow-mode grading: ``python -m screener.grade --date YYYY-MM-DD --results r.csv``.

Reloads a persisted :class:`DailyReport`, applies final match results (a manual
CSV to start), and answers two questions:

  1. PnL — for each FLAGGED edge, did the recommended side win, and what is the
     hypothetical profit/loss (Kalshi binary contracts pay 100¢ if your side
     resolves, 0 otherwise; you entered at the price of the side you backed)?
  2. CALIBRATION — across ALL priced markets (flagged and below-threshold), did
     markets the model called ~p% actually resolve true ~p% of the time? This is
     the real trust check: if the lambdas are off, the edges are noise.

No real money is ever involved. PnL is framed as "if you had bet 1 contract".
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import structlog
from pydantic import BaseModel, Field

from .config import Settings
from .logging import configure_logging
from .report import DailyReport, MatchReport, _edge_label
from .screening import EdgeSide, ScreenedMarket
from .settle import MatchResultInput, yes_resolves
from .storage import LocalReportStore

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class GradedEdge(BaseModel):
    match_id: str
    label: str
    side: EdgeSide
    entry_cents: int  # what you paid for the side you backed
    kalshi_cents: int
    fair_cents: int
    result: str  # "win" | "loss" | "void" | "ungradeable"
    pnl_cents: int = 0


class CalibrationBucket(BaseModel):
    lo: int  # bucket lower bound, percent
    hi: int
    n: int
    mean_predicted_pct: float
    actual_rate_pct: float


class GradeReport(BaseModel):
    report_date: date
    edges: list[GradedEdge] = Field(default_factory=list)
    calibration: list[CalibrationBucket] = Field(default_factory=list)
    # Overall model accuracy across all graded markets (lower = better; None if
    # nothing gradeable). 0 = perfect, 0.25 = an always-50% coin flip.
    brier: Optional[float] = None
    n_graded_markets: int = 0
    label: str = ""  # optional human label (e.g. a date range for aggregates)

    @property
    def graded(self) -> list[GradedEdge]:
        return [e for e in self.edges if e.result in ("win", "loss")]

    @property
    def total_pnl_cents(self) -> int:
        return sum(e.pnl_cents for e in self.edges)

    @property
    def wins(self) -> int:
        return sum(1 for e in self.edges if e.result == "win")

    @property
    def losses(self) -> int:
        return sum(1 for e in self.edges if e.result == "loss")


# --------------------------------------------------------------------------- #
# Grading
# --------------------------------------------------------------------------- #
def _grade_edge(
    sm: ScreenedMarket, match_id: str, result: Optional[MatchResultInput]
) -> GradedEdge:
    sel = sm.fair_value.selection
    side = sm.side or EdgeSide.YES
    kalshi = sm.kalshi_price_cents or 0
    fair = sm.fair_price_cents or 0
    # Entry price = price of the side we backed. Kalshi No price = 100 - Yes price.
    entry = kalshi if side == EdgeSide.YES else 100 - kalshi
    label = _edge_label(sm)

    base = GradedEdge(
        match_id=match_id, label=label, side=side,
        entry_cents=entry, kalshi_cents=kalshi, fair_cents=fair, result="ungradeable",
    )
    if result is None:
        return base
    yes_true = yes_resolves(sel, result)
    if yes_true is None:
        base.result = "void"
        return base
    # Our backed side wins when: YES bet & yes_true, or NO bet & not yes_true.
    resolved_for_us = yes_true if side == EdgeSide.YES else (not yes_true)
    base.pnl_cents = (100 - entry) if resolved_for_us else -entry
    base.result = "win" if resolved_for_us else "loss"
    return base


def brier_score(pairs: list[tuple[float, bool]]) -> Optional[float]:
    """Mean squared error of predicted probability vs outcome (0/1).

    The single-number summary of model quality: it rewards being both calibrated
    AND confident in the right direction. 0 = perfect; 0.25 = the score you'd get
    by predicting 50% on everything; higher = worse than a coin flip. Returns None
    if there's nothing graded yet.
    """
    if not pairs:
        return None
    return round(sum((p - (1.0 if o else 0.0)) ** 2 for p, o in pairs) / len(pairs), 4)


def _calibration(pairs: list[tuple[float, bool]]) -> list[CalibrationBucket]:
    """Decile calibration from (predicted_prob, outcome) pairs."""
    buckets: dict[int, list[tuple[float, bool]]] = {i: [] for i in range(10)}
    for p, o in pairs:
        buckets[min(int(p * 10), 9)].append((p, o))

    out: list[CalibrationBucket] = []
    for i in range(10):
        rows = buckets[i]
        if not rows:
            continue
        n = len(rows)
        out.append(
            CalibrationBucket(
                lo=i * 10, hi=i * 10 + 10, n=n,
                mean_predicted_pct=round(100 * sum(p for p, _ in rows) / n, 1),
                actual_rate_pct=round(100 * sum(1 for _, t in rows if t) / n, 1),
            )
        )
    return out


def _model_priced_markets(mr: MatchReport) -> list[ScreenedMarket]:
    """Every market the MODEL priced, regardless of whether Kalshi quoted a price.

    Calibration is about whether the model's probabilities are accurate, which is
    independent of Kalshi. A market the model priced but Kalshi didn't carries
    NO_PRICE status yet still records ``fair_price_cents`` — so we key off the
    presence of a fair value, not the screening status. This lets you validate
    the model against real results even before Kalshi's markets are liquid.
    (Corners / suppressed props have no model fair value -> excluded.)
    """
    return [
        sm
        for sm in (*mr.screen.flagged, *mr.screen.other)
        if sm.fair_price_cents is not None
    ]


def _collect(
    report: DailyReport, results: dict[str, MatchResultInput]
) -> tuple[list[GradedEdge], list[tuple[float, bool]]]:
    """Pull (graded edges, calibration pairs) from one day's report + results."""
    edges: list[GradedEdge] = []
    pairs: list[tuple[float, bool]] = []
    for mr in report.matches:
        match_id = mr.match.match_id
        result = results.get(match_id)
        for sm in mr.screen.flagged:
            edges.append(_grade_edge(sm, match_id, result))
        if result is None:
            continue
        for sm in _model_priced_markets(mr):
            yes_true = yes_resolves(sm.fair_value.selection, result)
            if yes_true is None or sm.fair_price_cents is None:
                continue
            pairs.append((sm.fair_price_cents / 100.0, yes_true))
    return edges, pairs


def _build_report(
    report_date: date, edges: list[GradedEdge], pairs: list[tuple[float, bool]], label: str = ""
) -> GradeReport:
    return GradeReport(
        report_date=report_date,
        edges=edges,
        calibration=_calibration(pairs),
        brier=brier_score(pairs),
        n_graded_markets=len(pairs),
        label=label,
    )


def grade(report: DailyReport, results: dict[str, MatchResultInput]) -> GradeReport:
    edges, pairs = _collect(report, results)
    return _build_report(report.report_date, edges, pairs)


def grade_many(items: list[tuple[DailyReport, dict[str, MatchResultInput]]]) -> GradeReport:
    """Aggregate grading across multiple days into one combined report — the
    statistically meaningful view, since per-day samples are small and noisy."""
    all_edges: list[GradedEdge] = []
    all_pairs: list[tuple[float, bool]] = []
    dates: list[date] = []
    for report, results in items:
        e, p = _collect(report, results)
        all_edges += e
        all_pairs += p
        dates.append(report.report_date)
    dates.sort()
    label = f"{dates[0].isoformat()} … {dates[-1].isoformat()} ({len(dates)} days)" if dates else "no days"
    return _build_report(dates[-1] if dates else date.today(), all_edges, all_pairs, label=label)


# --------------------------------------------------------------------------- #
# CSV input
# --------------------------------------------------------------------------- #
def read_results_csv(path: Path) -> dict[str, MatchResultInput]:
    """Read manual results. Columns: match_id,home_score,away_score[,ht_home,ht_away].

    Rows with a blank home/away score (match not played yet, or not filled in) are
    skipped — so a partially-completed CSV safely grades only the matches that
    have final scores. Extra columns (e.g. a 'matchup' label) are ignored.
    """
    out: dict[str, MatchResultInput] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            def _int(key: str) -> Optional[int]:
                v = (row.get(key) or "").strip()
                return int(v) if v else None

            mid = (row.get("match_id") or "").strip()
            hs = (row.get("home_score") or "").strip()
            as_ = (row.get("away_score") or "").strip()
            if not mid or not hs or not as_:
                continue
            advanced = (row.get("advanced") or "").strip().lower() or None
            out[mid] = MatchResultInput(
                match_id=mid,
                home_score=int(hs),
                away_score=int(as_),
                ht_home=_int("ht_home"),
                ht_away=_int("ht_away"),
                advanced=advanced if advanced in ("home", "away") else None,
            )
    return out


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_grade_markdown(g: GradeReport) -> str:
    L: list[str] = []
    header = g.label or g.report_date.isoformat()
    L.append(f"# Shadow-mode grade — {header}")
    L.append("")
    pnl = g.total_pnl_cents
    brier = "n/a" if g.brier is None else f"{g.brier:.4f}"
    L.append(
        f"_Graded {len(g.graded)} edge(s): **{g.wins}W / {g.losses}L**. "
        f"Hypothetical PnL at 1 contract each: **{pnl:+d}¢** (${pnl / 100:+.2f}). "
        "No real money; framing is 'if you had bet'._"
    )
    L.append("")
    L.append(
        f"_**Model Brier score: {brier}** across {g.n_graded_markets} graded market(s) "
        "(0 = perfect, 0.25 = always guessing 50%; lower is better)._"
    )
    L.append("")

    if g.edges:
        L.append("## Flagged edges")
        L.append("")
        L.append("| Match | Market | Side | Entry | Result | PnL |")
        L.append("|---|---|---|---|---|---|")
        for e in g.edges:
            L.append(
                f"| {e.match_id} | {e.label} | {e.side.value.upper()} | {e.entry_cents}¢ | "
                f"{e.result} | {e.pnl_cents:+d}¢ |"
            )
        L.append("")

    L.append("## Calibration (all model-priced markets)")
    L.append("")
    if not g.calibration:
        L.append("_No gradeable priced markets._")
    else:
        L.append("| Model prob bucket | N | Mean predicted | Actual hit rate |")
        L.append("|---|---|---|---|")
        for b in g.calibration:
            L.append(
                f"| {b.lo}–{b.hi}% | {b.n} | {b.mean_predicted_pct}% | {b.actual_rate_pct}% |"
            )
        L.append("")
        L.append(
            "_Well-calibrated means 'mean predicted' ≈ 'actual hit rate' in each "
            "bucket. Large, consistent gaps mean the lambdas are off and the edges "
            "are noise — review before trusting any output._"
        )
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _grade_all(store: LocalReportStore, root: Path) -> int:
    """Aggregate every date partition that has both a report and a results.csv."""
    items: list[tuple[DailyReport, dict[str, MatchResultInput]]] = []
    for d in sorted(root.glob("date=*")):
        results_csv = d / "results.csv"
        if not results_csv.exists():
            continue
        try:
            day = date.fromisoformat(d.name.split("=", 1)[1])
        except ValueError:
            continue
        items.append((store.load_report(day), read_results_csv(results_csv)))
    if not items:
        print(f"No graded days found under {root} (need date=*/results.csv).")
        return 1
    g = grade_many(items)
    print(render_grade_markdown(g))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screener.grade", description=__doc__)
    parser.add_argument("--date", help="match day to grade, YYYY-MM-DD")
    parser.add_argument("--results", help="path to results CSV (defaults to that day's results.csv)")
    parser.add_argument("--all", action="store_true", help="aggregate ALL graded days into one report")
    parser.add_argument("--output-dir", default=None, help="store root (default from settings)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    configure_logging(json=settings.log_json)
    root = Path(args.output_dir or settings.output_dir)
    store = LocalReportStore(root)

    if args.all:
        return _grade_all(store, root)

    if not args.date:
        parser.error("either --date YYYY-MM-DD or --all is required")
    target = date.fromisoformat(args.date)
    report = store.load_report(target)
    # Default the results CSV to the conventional per-day location.
    results_path = Path(args.results) if args.results else root / f"date={args.date}" / "results.csv"
    results = read_results_csv(results_path)

    grade_report = grade(report, results)
    md = render_grade_markdown(grade_report)
    print(md)
    store.save_artifact(target, "grade.md", md)
    store.save_artifact(target, "grade.json", grade_report.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
