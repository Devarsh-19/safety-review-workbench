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


def _llm_session_ids(conn) -> set:
    """
    In-scope session_ids that came through the LLM ingest path:
      * have an LLM-source flag (flagged LLM sessions), OR
      * reviewer_id = 'LLM' (clean, no-flag sessions auto-submitted by the ingest)
    Only ingest_llm_sessions.py produces either marker; data_loader does not.
    """
    ids = {r[0] for r in conn.execute(
        "SELECT session_id FROM rep_sessions WHERE reviewer_id = 'LLM'").fetchall()}
    ids |= {r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM rep_flags WHERE source = 'LLM'").fetchall()}
    return ids


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
def _sec_top_summary(conn, rep: Report) -> None:
    """
    Headline split by flag source, broken out by verdict (CLEAN/FLAGGED/SEVERE).
    Split by ingestion path, broken out by overall_verdict (CLEAN/FLAGGED/SEVERE)
    as children. Mutually exclusive; sum to ALL:
      LLM review    = has an LLM-source flag OR reviewer_id='LLM'
                      (ingested via ingest_llm_sessions.py — only that path writes
                       source='LLM' flags, and it stamps reviewer_id='LLM' on
                       clean auto-submitted sessions that have no flags)
      Manual review = everything else (data_loader path)
    """
    rep.heading("Summary — manual vs LLM by verdict")

    verdict_by_sid = {r[0]: (r[1] or "(null)") for r in conn.execute(
        "SELECT session_id, overall_verdict FROM rep_sessions").fetchall()}
    llm_sids = _llm_session_ids(conn)
    # Manual review = every in-scope session not from the LLM path.
    manual_sids = {s for s in verdict_by_sid if s not in llm_sids}

    # Always show the three standard verdicts as children, then any others present.
    present = set(verdict_by_sid.values())
    preferred = ["CLEAN", "FLAGGED", "SEVERE"]
    verdicts = preferred + sorted(present - set(preferred))

    def vcnt(sids, v):
        return sum(1 for s in sids if verdict_by_sid.get(s) == v)

    pairs = []
    for label, sids in (("Manual review (data_loader)", manual_sids),
                        ("LLM review (LLM flag or reviewer_id=LLM)", llm_sids)):
        pairs.append((label, _num(len(sids))))
        for v in verdicts:
            pairs.append((f"    {v}", _num(vcnt(sids, v))))
    pairs.append(("ALL sessions", _num(len(verdict_by_sid))))
    rep.kv(pairs)
    rep.note("LLM review = has LLM-source flag OR reviewer_id 'LLM' (ingest_llm); "
             "Manual review = all others (data_loader). Mutually exclusive; sum to ALL.")


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


def _sec_astro_fp_fn(conn, rep: Report) -> None:
    """
    AstroTalk false positives / negatives vs our review (active flags only,
    re-engagement excluded; all scoped to SUBMITTED_FOR_REVIEW + LOCKED):

      FALSE NEGATIVE = AstroTalk clean (astrotalk_flagged=0) but WE flagged it,
                       i.e. the session has an active flag. Split by flag source
                       (MANUAL = us, LLM = the model). The two can overlap.
      FALSE POSITIVE = AstroTalk flagged (astrotalk_flagged=1) but our verdict is
                       CLEAN. Split by who cleared it (reviewer_id='LLM' vs human).
      Clean by both  = AstroTalk clean AND our verdict CLEAN.
    """
    rep.heading("AstroTalk false positives / negatives")

    # FALSE NEGATIVE — astro clean, we flagged (has an active flag).
    def fn_by(src_clause: str) -> int:
        return _scalar(
            conn,
            "SELECT COUNT(DISTINCT f.session_id) FROM rep_flags f "
            "JOIN rep_sessions s ON s.session_id = f.session_id "
            "WHERE s.astrotalk_flagged = 0" + src_clause,
        )

    fn_total  = fn_by("")
    fn_manual = fn_by(" AND f.source = 'MANUAL'")
    fn_llm    = fn_by(" AND f.source = 'LLM'")

    # FALSE POSITIVE — astro flagged, our verdict CLEAN.
    fp_total = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE astrotalk_flagged=1 AND overall_verdict='CLEAN'")
    fp_llm   = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE astrotalk_flagged=1 AND overall_verdict='CLEAN' AND reviewer_id='LLM'")
    fp_manual = fp_total - fp_llm

    both = _scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE astrotalk_flagged=0 AND overall_verdict='CLEAN'")

    rep.kv([
        ("FALSE NEGATIVE — AstroTalk clean, we flagged", _num(fn_total)),
        ("    by Manual flag (source=MANUAL)", _num(fn_manual)),
        ("    by LLM flag (source=LLM)", _num(fn_llm)),
        ("FALSE POSITIVE — AstroTalk flagged, verdict CLEAN", _num(fp_total)),
        ("    cleared by Manual review", _num(fp_manual)),
        ("    cleared by LLM review (reviewer_id=LLM)", _num(fp_llm)),
        ("Clean by both (AstroTalk clean + verdict CLEAN)", _num(both)),
    ])
    rep.note("FN by source may overlap (a session can have both MANUAL and LLM flags). "
             "Active flags only; re-engagement excluded.")


def _sec_confusion(conn, rep: Report) -> None:
    rep.heading("AstroTalk accuracy (vs our review)")
    # Same definitions as the FP/FN section, so the two never disagree:
    #   we_flagged = session has an active (non-re-engagement) flag
    #   FP / clean-by-both keyed on overall_verdict = 'CLEAN'
    flagged_by_us = {r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM rep_flags").fetchall()}

    tp = fp = fn = tn = 0
    for r in conn.execute(
        "SELECT session_id, astrotalk_flagged, overall_verdict FROM rep_sessions"
    ).fetchall():
        af = r["astrotalk_flagged"]
        if af not in (0, 1):
            continue
        we_flagged = r["session_id"] in flagged_by_us
        if   af == 1 and we_flagged:                    tp += 1
        elif af == 1 and r["overall_verdict"] == "CLEAN": fp += 1
        elif af == 0 and we_flagged:                    fn += 1
        elif af == 0 and r["overall_verdict"] == "CLEAN": tn += 1
    scored = tp + fp + fn + tn

    rep.kv([
        ("Scored sessions", _num(scored)),
        ("True  Positive (TP) — astro flagged & we flagged", _num(tp)),
        ("False Positive (FP) — astro flagged & verdict CLEAN", _num(fp)),
        ("False Negative (FN) — astro clean & we flagged", _num(fn)),
        ("True  Negative (TN) — astro clean & verdict CLEAN", _num(tn)),
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

    # 'LLM' and 'AUTO_LOCK' are non-human actors — excluded from reviewer workload.
    assigned  = to_map("SELECT assigned_to, COUNT(*) FROM rep_sessions WHERE assigned_to IS NOT NULL AND assigned_to != 'LLM' GROUP BY assigned_to")
    submitted = to_map("SELECT submitted_by, COUNT(*) FROM rep_sessions WHERE submitted_by IS NOT NULL AND submitted_by != 'LLM' GROUP BY submitted_by")
    locked    = to_map("SELECT locked_by, COUNT(*) FROM rep_sessions WHERE review_status='LOCKED' AND locked_by IS NOT NULL AND locked_by != 'AUTO_LOCK' GROUP BY locked_by")

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


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_report() -> Report:
    conn = get_connection()
    rep = Report()
    try:
        scope_sql = ", ".join(f"'{s}'" for s in SCOPE_STATUSES)
        excl_sql  = ", ".join(f"'{c.upper()}'" for c in EXCLUDED_FLAG_CATEGORIES)
        # Normalised form for matching: lower-case, '-'/space -> '_'.
        excl_norm = ", ".join(
            "'" + c.lower().replace('-', '_').replace(' ', '_') + "'"
            for c in EXCLUDED_FLAG_CATEGORIES
        )
        # rep_flags = ACTIVE, non-excluded flags for in-scope sessions:
        #   * active  -> drop amended-original rows (flag_id referenced as a parent)
        #   * exclude -> re-engagement etc., matched on a normalised category_code
        conn.executescript(
            f"""
            DROP VIEW IF EXISTS rep_sessions;
            DROP VIEW IF EXISTS rep_flags;
            CREATE TEMP VIEW rep_sessions AS
                SELECT * FROM sessions WHERE review_status IN ({scope_sql});
            CREATE TEMP VIEW rep_flags AS
                SELECT f.* FROM flags f
                WHERE f.session_id IN (SELECT session_id FROM rep_sessions)
                  AND LOWER(REPLACE(REPLACE(f.category_code,'-','_'),' ','_')) NOT IN ({excl_norm})
                  AND f.flag_id NOT IN (SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL);
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
            ("Manually submitted (L1, ever)", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE submitted_by IS NOT NULL AND submitted_by != 'LLM'"))),
            ("LLM auto-submitted (ever)", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE submitted_by = 'LLM'"))),
            ("Currently SUBMITTED_FOR_REVIEW", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE review_status='SUBMITTED_FOR_REVIEW'"))),
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
        _sec_astro_fp_fn(conn, rep)
        _sec_confusion(conn, rep)

        rep.heading("LLM run")
        rep.kv([
            ("LLM-ingested sessions", _num(len(_llm_session_ids(conn)))),
            ("  - with an LLM-source flag", _num(_scalar(conn, "SELECT COUNT(DISTINCT session_id) FROM rep_flags WHERE source='LLM'"))),
            ("  - clean auto-submit (reviewer_id=LLM)", _num(_scalar(conn, "SELECT COUNT(*) FROM rep_sessions WHERE reviewer_id='LLM'"))),
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
