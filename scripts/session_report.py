"""
session_report.py

Read-only summary report over the results database. Prints, count-wise:

  * Totals (sessions, flags)
  * Submission        — manually submitted / currently submitted for review
  * Verdict           — overall_verdict breakdown (CLEAN / FLAGGED / SEVERE / ...)
  * AstroTalk signal  — flagged vs clean, by category and severity
  * Signal agreement  — AstroTalk vs LLM/reviewer verdict (2-way matrix)
  * AstroTalk accuracy— confusion matrix (FP / FN) vs reviewed verdict
  * LLM run           — sessions processed by the LLM pipeline
  * Queue / backlog   — pending (assigned/unassigned), submitted, needs-final
  * Review status     — review_status breakdown
  * Reviewer workload — assigned / submitted / locked per reviewer
  * Locks             — locked, split by who locked them
  * Auto-lock impact  — auto vs manual locks, and unlocked candidates
  * Flags             — flag counts by category, source, status, severity
  * Flags per session — distribution of flag counts across sessions
  * Flag author       — flagged sessions by turn speaker (astrologer vs user)
  * Language          — volume and flag rate per language
  * Anomalies         — null / unprocessed / lowercase data-quality counts

The whole report is scoped to SCOPE_STATUSES and excludes EXCLUDED_FLAG_CATEGORIES.
Read-only: never writes to the database.

Usage:
  python scripts/session_report.py                       # print to console
  python scripts/session_report.py --docx report.docx    # also write a Word file
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402

# A session counts as "flagged" when the LLM/reviewer verdict is adverse.
FLAGGED_VERDICTS = ("FLAGGED", "SEVERE")

# The whole report is scoped to sessions in these review states only.
# (AUTO_LOCK is a locked_by value within LOCKED, so it is already covered.)
SCOPE_STATUSES = ("SUBMITTED_FOR_REVIEW", "LOCKED")

# Flag categories excluded from every flag-based count (matched case-insensitively).
EXCLUDED_FLAG_CATEGORIES = ("RE_ENGAGEMENT_SOLICITATION",)


# ---------------------------------------------------------------------------
# Structured report model — sections append blocks; renderers consume them.
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.blocks = []        # list of (kind, *payload)

    def meta(self, pairs):      self.blocks.append(("meta", pairs))
    def heading(self, text):    self.blocks.append(("heading", text))
    def kv(self, pairs):        self.blocks.append(("kv", pairs))
    def table(self, headers, rows): self.blocks.append(("table", headers, rows))
    def note(self, text):       self.blocks.append(("note", text))


def _num(n) -> str:
    return f"{n:,}" if isinstance(n, int) else str(n)


def _kv_from_rows(rows):
    """[(label, count), ...] -> [(label-or-(null), formatted-count), ...]."""
    return [("(null)" if r[0] is None else str(r[0]), _num(r[1])) for r in rows]


def _scalar(conn, sql, params=()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _sec_top_summary(conn, rep: Report) -> None:
    """
    Headline split by flag source, each broken into submitted-for-review and
    locked. Classification (mutually exclusive; clean sessions are in neither):
      LLM    = session has >=1 LLM-source flag (may also have manual flags)
      Manual = session has flags and ALL of them are MANUAL-source (no LLM flag)
    (Re-engagement flags are excluded, like everywhere else in the report.)
    """
    rep.heading("Summary — manual vs LLM (submitted / locked)")

    llm_sids = {r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM rep_flags WHERE source='LLM'").fetchall()}
    manual_sids = {r[0] for r in conn.execute(
        "SELECT session_id FROM rep_flags GROUP BY session_id "
        "HAVING COUNT(*) = SUM(CASE WHEN source='MANUAL' THEN 1 ELSE 0 END)").fetchall()}
    flagged_sids = {r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM rep_flags").fetchall()}
    status_by_sid = {r[0]: r[1] for r in conn.execute(
        "SELECT session_id, review_status FROM rep_sessions").fetchall()}
    # Clean = in-scope session with no (non-re-engagement) flags.
    clean_sids = {s for s in status_by_sid if s not in flagged_sids}

    def cnt(sids, status):
        return sum(1 for s in sids if status_by_sid.get(s) == status)

    rows = []
    for label, sids in (("Clean (no flags)", clean_sids),
                        ("Manual (manual flags only)", manual_sids),
                        ("LLM (has LLM flag)", llm_sids)):
        sub = cnt(sids, "SUBMITTED_FOR_REVIEW")
        lock = cnt(sids, "LOCKED")
        rows.append([label, _num(sub), _num(lock), _num(sub + lock)])
    # All-categories total row.
    all_sub = cnt(status_by_sid, "SUBMITTED_FOR_REVIEW")
    all_lock = cnt(status_by_sid, "LOCKED")
    rows.append(["ALL sessions", _num(all_sub), _num(all_lock), _num(all_sub + all_lock)])
    rep.table(["CATEGORY", "SUBMITTED_FOR_REVIEW", "LOCKED", "TOTAL"], rows)
    rep.note("Clean = no flags; Manual = all flags MANUAL; LLM = has >=1 LLM flag. "
             "Clean/Manual/LLM are mutually exclusive and sum to ALL.")


def _sec_signal_agreement(conn, rep: Report) -> None:
    rep.heading("Signal agreement (AstroTalk x verdict)")
    rows = conn.execute(
        """SELECT
             CASE WHEN astrotalk_flagged = 1 THEN 'astro_flagged'
                  WHEN astrotalk_flagged = 0 THEN 'astro_clean'
                  ELSE 'astro_null' END AS astro,
             COALESCE(overall_verdict, '(null)') AS verdict,
             COUNT(*) AS n
           FROM rep_sessions GROUP BY astro, verdict"""
    ).fetchall()

    astro_rows = ["astro_flagged", "astro_clean", "astro_null"]
    verdicts = sorted({r["verdict"] for r in rows})
    grid = {(r["astro"], r["verdict"]): r["n"] for r in rows}

    headers = [""] + verdicts + ["TOTAL"]
    out_rows = []
    for a in astro_rows:
        cells = [grid.get((a, v), 0) for v in verdicts]
        row_total = sum(cells)
        if row_total or a != "astro_null":
            out_rows.append([a] + [_num(c) for c in cells] + [_num(row_total)])
    rep.table(headers, out_rows)

    fp = sum(grid.get(("astro_flagged", v), 0) for v in verdicts if v == "CLEAN")
    miss = sum(grid.get(("astro_clean", v), 0) for v in verdicts if v in FLAGGED_VERDICTS)
    rep.note(f"AstroTalk flagged but verdict CLEAN (cleared FPs): {fp:,}")
    rep.note(f"AstroTalk clean but verdict FLAGGED/SEVERE (misses): {miss:,}")


def _sec_confusion(conn, rep: Report) -> None:
    rep.heading("AstroTalk accuracy (vs reviewed verdict)")
    pos = "('FLAGGED','SEVERE')"

    def n(where):
        return _scalar(
            conn,
            f"SELECT COUNT(*) FROM rep_sessions "
            f"WHERE astrotalk_flagged IN (0,1) AND overall_verdict IN ('CLEAN','FLAGGED','SEVERE') "
            f"AND {where}",
        )

    tp = n(f"astrotalk_flagged=1 AND overall_verdict IN {pos}")
    fp = n("astrotalk_flagged=1 AND overall_verdict = 'CLEAN'")
    fn = n(f"astrotalk_flagged=0 AND overall_verdict IN {pos}")
    tn = n("astrotalk_flagged=0 AND overall_verdict = 'CLEAN'")
    scored = tp + fp + fn + tn

    rep.kv([
        ("Scored sessions (excl. UNPROCESSED/null)", _num(scored)),
        ("True  Positive (TP) — flagged & bad", _num(tp)),
        ("False Positive (FP) — flagged & CLEAN", _num(fp)),
        ("False Negative (FN) — clean & bad (missed)", _num(fn)),
        ("True  Negative (TN) — clean & CLEAN", _num(tn)),
    ])

    precision = tp / (tp + fp) if (tp + fp) else None
    recall    = tp / (tp + fn) if (tp + fn) else None
    fpr       = fp / (fp + tn) if (fp + tn) else None
    fnr       = fn / (fn + tp) if (fn + tp) else None
    acc       = (tp + tn) / scored if scored else None

    def f(x):
        return f"{100*x:.1f}%" if x is not None else "n/a"

    rep.kv([
        ("Precision  TP/(TP+FP)", f(precision)),
        ("Recall     TP/(TP+FN)", f(recall)),
        ("False-positive rate FP/(FP+TN)", f(fpr)),
        ("False-negative rate FN/(FN+TP)", f(fnr)),
        ("Accuracy   (TP+TN)/all", f(acc)),
    ])


def _sec_queue(conn, rep: Report) -> None:
    rep.heading("Queue / backlog")
    pending_total = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='PENDING'")
    pending_assigned = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='PENDING' AND assigned_to IS NOT NULL")
    submitted = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='SUBMITTED_FOR_REVIEW'")
    needs_final = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='NEEDS_FINAL_REVIEW'")
    rep.kv([
        ("PENDING (total)", _num(pending_total)),
        ("  - assigned", _num(pending_assigned)),
        ("  - unassigned", _num(pending_total - pending_assigned)),
        ("SUBMITTED_FOR_REVIEW", _num(submitted)),
        ("NEEDS_FINAL_REVIEW", _num(needs_final)),
    ])


def _sec_reviewer_workload(conn, rep: Report) -> None:
    rep.heading("Reviewer workload")

    def to_map(sql):
        return {r[0]: r[1] for r in conn.execute(sql).fetchall() if r[0] is not None}

    assigned  = to_map("SELECT assigned_to, COUNT(*) FROM rep_sessions WHERE assigned_to IS NOT NULL GROUP BY assigned_to")
    submitted = to_map("SELECT submitted_by, COUNT(*) FROM rep_sessions WHERE submitted_by IS NOT NULL GROUP BY submitted_by")
    locked    = to_map("SELECT locked_by, COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' AND locked_by IS NOT NULL GROUP BY locked_by")

    names = sorted(set(assigned) | set(submitted) | set(locked))
    if not names:
        rep.note("(no reviewer activity)")
        return
    rows = [[name, _num(assigned.get(name, 0)), _num(submitted.get(name, 0)), _num(locked.get(name, 0))]
            for name in names]
    rep.table(["REVIEWER", "ASSIGNED", "SUBMITTED", "LOCKED"], rows)


def _sec_auto_lock(conn, rep: Report) -> None:
    rep.heading("Auto-lock impact")
    locked_auto = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' AND locked_by='AUTO_LOCK'")
    locked_manual = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' AND locked_by IS NOT NULL AND locked_by!='AUTO_LOCK'")
    locked_null = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' AND locked_by IS NULL")
    candidates = _scalar(
        conn,
        "SELECT COUNT(*) FROM rep_sessions WHERE overall_verdict='CLEAN' AND astrotalk_flagged=0 "
        "AND review_status='SUBMITTED_FOR_REVIEW'",
    )
    rep.kv([
        ("Locked by AUTO_LOCK", _num(locked_auto)),
        ("Locked manually", _num(locked_manual)),
        ("Locked (locked_by null)", _num(locked_null)),
        ("Unlocked auto-lock candidates", _num(candidates)),
    ])
    rep.note("Candidates = CLEAN + astrotalk_flagged=0 + SUBMITTED_FOR_REVIEW")


def _sec_flags_per_session(conn, rep: Report, total_sessions: int, total_flags: int) -> None:
    rep.heading("Flags per session")
    rows = conn.execute(
        "SELECT cnt, COUNT(*) FROM (SELECT session_id, COUNT(*) cnt FROM rep_flags GROUP BY session_id) GROUP BY cnt"
    ).fetchall()
    per = {r[0]: r[1] for r in rows}
    sessions_with_flags = sum(per.values())
    avg = (total_flags / sessions_with_flags) if sessions_with_flags else 0
    rep.kv([
        ("0 flags", _num(total_sessions - sessions_with_flags)),
        ("1 flag", _num(per.get(1, 0))),
        ("2 flags", _num(per.get(2, 0))),
        ("3+ flags", _num(sum(c for k, c in per.items() if k >= 3))),
        ("avg flags / flagged session", f"{avg:.2f}"),
    ])


def _sec_flag_author(conn, rep: Report) -> None:
    rep.heading("Flagged sessions by flag author (turn speaker)")

    def sids(speaker):
        return {
            r[0] for r in conn.execute(
                "SELECT DISTINCT f.session_id FROM rep_flags f "
                "JOIN turns t ON f.session_id = t.session_id AND f.turn_id = t.turn_id "
                "WHERE t.speaker = ?",
                (speaker,),
            ).fetchall()
        }

    astro = sids("ASTROLOGER")
    user  = sids("USER")
    flagged_total = _scalar(conn, "SELECT COUNT(DISTINCT session_id) FROM rep_flags")
    rep.kv([
        ("Flagged sessions (have >=1 flag)", _num(flagged_total)),
        ("- with an ASTROLOGER-turn flag", _num(len(astro))),
        ("- with a USER-turn flag", _num(len(user))),
        ("- mixed (both speakers flagged)", _num(len(astro & user))),
        ("- ONLY astrologer-turn flags", _num(len(astro - user))),
        ("- ONLY user-turn flags", _num(len(user - astro))),
    ])


def _sec_rate_table(conn, rep: Report, title: str, col: str, label: str) -> None:
    rep.heading(title)
    rows = conn.execute(
        f"""SELECT COALESCE({col}, '(null)') AS k,
                   COUNT(*) AS total,
                   SUM(CASE WHEN overall_verdict IN {FLAGGED_VERDICTS} THEN 1 ELSE 0 END) AS flagged
            FROM rep_sessions GROUP BY k ORDER BY total DESC"""
    ).fetchall()
    if not rows:
        rep.note("(none)")
        return
    out = [[r["k"], _num(r["total"]), _num(r["flagged"]), _pct(r["flagged"], r["total"])] for r in rows]
    rep.table([label, "TOTAL", "FLAGGED", "RATE"], out)


def _sec_anomalies(conn, rep: Report) -> None:
    rep.heading("Anomalies / data quality")
    checks = [
        ("astrotalk_flagged IS NULL", "SELECT COUNT(*) FROM rep_sessions WHERE astrotalk_flagged IS NULL"),
        ("verdict UNPROCESSED/null", "SELECT COUNT(*) FROM rep_sessions WHERE overall_verdict IS NULL OR overall_verdict='UNPROCESSED'"),
        ("LOCKED with locked_by null", "SELECT COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' AND locked_by IS NULL"),
        ("flags with source null", "SELECT COUNT(*) FROM rep_flags WHERE source IS NULL"),
        ("flags with status null", "SELECT COUNT(*) FROM rep_flags WHERE status IS NULL"),
        ("flags category_code lowercase", "SELECT COUNT(*) FROM rep_flags WHERE category_code GLOB '*[a-z]*'"),
        ("sessions verdict lowercase", "SELECT COUNT(*) FROM rep_sessions WHERE overall_verdict GLOB '*[a-z]*'"),
    ]
    rep.kv([(label, _num(_scalar(conn, sql))) for label, sql in checks])


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_report() -> Report:
    conn = get_connection()
    rep = Report()
    try:
        scope_sql = ", ".join(f"'{s}'" for s in SCOPE_STATUSES)
        excl_sql = ", ".join(f"'{c.upper()}'" for c in EXCLUDED_FLAG_CATEGORIES)
        conn.executescript(
            f"""
            DROP VIEW IF EXISTS rep_sessions;
            DROP VIEW IF EXISTS rep_flags;
            CREATE TEMP VIEW rep_sessions AS
                SELECT * FROM sessions WHERE review_status IN ({scope_sql});
            CREATE TEMP VIEW rep_flags AS
                SELECT f.* FROM flags f
                WHERE f.session_id IN (SELECT session_id FROM rep_sessions)
                  AND UPPER(f.category_code) NOT IN ({excl_sql});
            """
        )

        rep.meta([
            ("Database", DB_PATH),
            ("Scope", f"review_status IN ({scope_sql})"),
            ("Excluded", f"flag category {excl_sql}"),
        ])

        total_sessions = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions")
        total_flags    = _scalar(conn, "SELECT COUNT(*) FROM rep_flags")

        _sec_top_summary(conn, rep)

        rep.heading("Totals")
        rep.kv([("Total sessions", _num(total_sessions)), ("Total flags", _num(total_flags))])

        rep.heading("Submission")
        rep.kv([
            ("Manually submitted (ever)", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE submitted_by IS NOT NULL"))),
            ("Currently submitted", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='SUBMITTED_FOR_REVIEW'"))),
        ])

        rep.heading("Verdict (overall_verdict)")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT overall_verdict, COUNT(*) FROM rep_sessions GROUP BY overall_verdict ORDER BY COUNT(*) DESC"
        ).fetchall()))

        rep.heading("AstroTalk signal (astrotalk_flagged)")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT CASE WHEN astrotalk_flagged=1 THEN 'flagged (1)' "
            "            WHEN astrotalk_flagged=0 THEN 'clean (0)' "
            "            ELSE 'unknown (null)' END AS sig, COUNT(*) "
            "FROM rep_sessions GROUP BY sig ORDER BY COUNT(*) DESC"
        ).fetchall()))

        rep.heading("AstroTalk flag category")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT astrotalk_flag_category, COUNT(*) FROM rep_sessions "
            "WHERE astrotalk_flagged=1 GROUP BY astrotalk_flag_category ORDER BY COUNT(*) DESC"
        ).fetchall()))

        rep.heading("AstroTalk severity")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT astrotalk_severity, COUNT(*) FROM rep_sessions "
            "WHERE astrotalk_flagged=1 GROUP BY astrotalk_severity ORDER BY COUNT(*) DESC"
        ).fetchall()))

        _sec_signal_agreement(conn, rep)
        _sec_confusion(conn, rep)

        rep.heading("LLM run")
        rep.kv([
            ("Sessions processed by LLM", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE overall_verdict IS NOT NULL AND overall_verdict!='UNPROCESSED'"))),
            ("Sessions not yet processed", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE overall_verdict IS NULL OR overall_verdict='UNPROCESSED'"))),
            ("Sessions with LLM flags", _num(_scalar(conn, "SELECT COUNT(DISTINCT session_id) FROM rep_flags WHERE source='LLM'"))),
        ])

        _sec_queue(conn, rep)

        rep.heading("Review status (review_status)")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT review_status, COUNT(*) FROM rep_sessions GROUP BY review_status ORDER BY COUNT(*) DESC"
        ).fetchall()))

        _sec_reviewer_workload(conn, rep)

        rep.heading("Locks (locked_by, for LOCKED sessions)")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT locked_by, COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' GROUP BY locked_by ORDER BY COUNT(*) DESC"
        ).fetchall()))

        _sec_auto_lock(conn, rep)

        rep.heading("Flags by category (category_code)")
        rep.kv(_kv_from_rows(conn.execute(
            "SELECT category_code, COUNT(*) FROM rep_flags GROUP BY category_code ORDER BY COUNT(*) DESC"
        ).fetchall()))

        rep.heading("Flags by source")
        rep.kv(_kv_from_rows(conn.execute("SELECT source, COUNT(*) FROM rep_flags GROUP BY source ORDER BY COUNT(*) DESC").fetchall()))

        rep.heading("Flags by status")
        rep.kv(_kv_from_rows(conn.execute("SELECT status, COUNT(*) FROM rep_flags GROUP BY status ORDER BY COUNT(*) DESC").fetchall()))

        rep.heading("Flags by severity")
        rep.kv(_kv_from_rows(conn.execute("SELECT severity, COUNT(*) FROM rep_flags GROUP BY severity ORDER BY COUNT(*) DESC").fetchall()))

        _sec_flags_per_session(conn, rep, total_sessions, total_flags)
        _sec_flag_author(conn, rep)
        _sec_rate_table(conn, rep, "Language (volume + flag rate)", "language_code", "LANGUAGE")
        _sec_anomalies(conn, rep)

        return rep
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def render_text(rep: Report) -> None:
    print("=" * 60)
    print("  Session / Flag Report")
    print("=" * 60)
    for block in rep.blocks:
        kind = block[0]
        if kind == "meta":
            for k, v in block[1]:
                print(f"  {k:<9}: {v}")
        elif kind == "heading":
            print()
            print(block[1])
            print("-" * len(block[1]))
        elif kind == "kv":
            pairs = block[1]
            if not pairs:
                print("  (none)")
                continue
            lw = max(len(str(l)) for l, _ in pairs)
            vw = max(len(str(v)) for _, v in pairs)
            for l, v in pairs:
                print(f"  {str(l):<{lw}}   {str(v):>{vw}}")
        elif kind == "table":
            headers, rows = block[1], block[2]
            widths = [len(str(h)) for h in headers]
            for r in rows:
                for i, c in enumerate(r):
                    widths[i] = max(widths[i], len(str(c)))

            def fmtrow(cells):
                parts = []
                for i, c in enumerate(cells):
                    parts.append(f"{str(c):<{widths[i]}}" if i == 0 else f"{str(c):>{widths[i]}}")
                return "  " + "  ".join(parts)

            print(fmtrow(headers))
            for r in rows:
                print(fmtrow(r))
        elif kind == "note":
            print(f"  {block[1]}")
    print()


def render_docx(rep: Report, path: str) -> None:
    from docx import Document as Docx
    from docx.shared import Pt, RGBColor

    d = Docx()
    d.add_heading("Session / Flag Report", level=0)

    def kv_table(pairs):
        t = d.add_table(rows=0, cols=2)
        t.style = "Light Grid Accent 1"
        for l, v in pairs:
            cells = t.add_row().cells
            cells[0].text = str(l)
            cells[1].text = str(v)
        return t

    for block in rep.blocks:
        kind = block[0]
        if kind == "meta":
            for k, v in block[1]:
                p = d.add_paragraph()
                run = p.add_run(f"{k}: ")
                run.bold = True
                p.add_run(str(v))
        elif kind == "heading":
            d.add_heading(block[1], level=1)
        elif kind == "kv":
            if not block[1]:
                d.add_paragraph("(none)")
            else:
                kv_table(block[1])
        elif kind == "table":
            headers, rows = block[1], block[2]
            t = d.add_table(rows=1, cols=len(headers))
            t.style = "Light Grid Accent 1"
            for i, h in enumerate(headers):
                cell = t.rows[0].cells[i]
                cell.text = str(h)
                for r in cell.paragraphs[0].runs:
                    r.bold = True
            for row in rows:
                cells = t.add_row().cells
                for i, c in enumerate(row):
                    cells[i].text = str(c)
        elif kind == "note":
            p = d.add_paragraph()
            run = p.add_run(str(block[1]))
            run.italic = True

    d.save(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only session/flag summary report.")
    parser.add_argument("--docx", metavar="PATH", help="Also write the report to a .docx file.")
    parser.add_argument("--no-text", action="store_true", help="Suppress console output (use with --docx).")
    args = parser.parse_args()

    rep = build_report()
    if not args.no_text:
        render_text(rep)
    if args.docx:
        render_docx(rep, args.docx)
        print(f"Wrote Word report to {args.docx}")


if __name__ == "__main__":
    main()
