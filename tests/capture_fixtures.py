#!/usr/bin/env python3
"""One-off: capture a small set of real Banner HTML pages as test fixtures.

Run with:  uv run python tests/capture_fixtures.py

Makes a handful of requests (1 semester list, 1 subject list, 1 course search,
1 catalog page, a few section details) against the latest term only, and writes
the raw HTML into tests/fixtures/. Re-run only when you intentionally want to
refresh the fixtures.
"""

import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import crawl  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


async def main():
    FIXTURES.mkdir(exist_ok=True)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(120.0),
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "AUSCrawl/2.0 (fixture-capture)"},
    ) as client:
        # Semester list
        resp = await crawl.request_with_retry(client, "GET", crawl.ENDPOINTS["semesters"])
        (FIXTURES / "semesters.html").write_text(resp.text, encoding="utf-8")
        semesters = await crawl.fetch_semesters(client)
        latest = semesters[-1]
        print(f"Latest term: {latest.term_id} {latest.term_name}")

        # Subject list for latest term
        resp = await crawl.request_with_retry(
            client, "POST", crawl.ENDPOINTS["subjects"],
            form={"p_calling_proc": "bwckschd.p_disp_dyn_sched", "p_term": latest.term_id},
        )
        (FIXTURES / "subjects.html").write_text(resp.text, encoding="utf-8")
        subjects = await crawl.fetch_subjects(client, latest.term_id)
        print(f"{len(subjects)} subjects; sampling one for course/catalog pages")

        # Pick a subject likely to have a modest number of courses.
        preferred = ["PHI", "WRI", "ARA", "PHY", "BIO"]
        codes = [s.short_name for s in subjects]
        subj = next((c for c in preferred if c in codes), codes[0])
        print(f"Using subject: {subj}")

        # Course search for that single subject, latest term
        params = crawl.build_course_params(latest.term_id, [subj])
        resp = await crawl.request_with_retry(
            client, "POST", crawl.ENDPOINTS["courses"], form=params
        )
        (FIXTURES / "courses.html").write_text(resp.text, encoding="utf-8")
        courses = crawl.parse_courses(resp.text, latest.term_id)
        print(f"Parsed {len(courses)} course rows from {subj}")

        # Catalog page for that subject
        resp = await crawl.request_with_retry(
            client, "GET", crawl.ENDPOINTS["catalog"],
            params={
                "term_in": latest.term_id, "one_subj": subj,
                "sel_crse_strt": "0", "sel_crse_end": "9999",
                "sel_subj": "", "sel_levl": "", "sel_schd": "",
                "sel_coll": "", "sel_divs": "", "sel_dept": "", "sel_attr": "",
            },
        )
        (FIXTURES / "catalog.html").write_text(resp.text, encoding="utf-8")

        # A few section detail pages, preferring CRNs that have prerequisites.
        seen_crns = []
        for c in courses:
            if c.crn not in seen_crns:
                seen_crns.append(c.crn)
        captured = 0
        for crn in seen_crns:
            resp = await crawl.request_with_retry(
                client, "GET", crawl.ENDPOINTS["detail"],
                params={"term_in": latest.term_id, "crn_in": crn},
            )
            text = resp.text
            has_prereq = "Prerequisites" in text or "Corequisites" in text
            name = f"detail_{crn}.html"
            (FIXTURES / name).write_text(text, encoding="utf-8")
            captured += 1
            print(f"  detail {crn}: prereq={has_prereq} -> {name}")
            if captured >= 5 and any(
                "Prerequisites" in (FIXTURES / f"detail_{c}.html").read_text(encoding="utf-8")
                for c in seen_crns[:captured]
            ):
                break
            if captured >= 8:
                break

        # Record which term/subject/crns we used
        (FIXTURES / "manifest.txt").write_text(
            f"term_id={latest.term_id}\n"
            f"subject={subj}\n"
            f"crns={','.join(seen_crns[:captured])}\n",
            encoding="utf-8",
        )
        print(f"Captured {captured} detail pages. Fixtures in {FIXTURES}")


if __name__ == "__main__":
    asyncio.run(main())
