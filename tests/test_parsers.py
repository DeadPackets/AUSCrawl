"""Characterization/regression tests for the HTML parsers (finding #14).

These run against real Banner HTML captured in tests/fixtures/. The detail-page
test compares against a golden baseline so the #9 lxml refactor must reproduce
the previous output exactly.
"""

import dataclasses
import json
import re

import crawl
from conftest import read_fixture


# ── parse_title ─────────────────────────────────────────────────────────────


def test_parse_title_basic():
    title, crn, subj, num, section = crawl.parse_title(
        "Introduction to Philosophy - 11179 - PHI 201 - 01"
    )
    assert (title, crn, subj, num, section) == (
        "Introduction to Philosophy", "11179", "PHI", "201", "01"
    )


def test_parse_title_keeps_dashes_inside_title():
    title, crn, subj, num, section = crawl.parse_title(
        "Special Topics - Ethics & AI - 12345 - PHI 490 - 02"
    )
    assert title == "Special Topics - Ethics & AI"
    assert (crn, subj, num, section) == ("12345", "PHI", "490", "02")


def test_parse_title_too_few_parts_is_safe():
    assert crawl.parse_title("No structure here") == ("No structure here", "", "", "", "")


# ── decode_cf_email ─────────────────────────────────────────────────────────


def test_decode_cf_email_on_real_token():
    html = read_fixture("courses.html")
    m = re.search(r"/cdn-cgi/l/email-protection#([a-fA-F0-9]+)", html)
    assert m, "expected at least one Cloudflare-protected email in the fixture"
    decoded = crawl.decode_cf_email(m.group(1))
    assert decoded.endswith("@aus.edu")
    assert "@" in decoded and " " not in decoded


def test_decode_cf_email_garbage_is_safe():
    assert crawl.decode_cf_email("zz") == ""
    assert crawl.decode_cf_email("") == ""


# ── parse_courses ───────────────────────────────────────────────────────────


def test_parse_courses_extracts_known_section(manifest):
    courses = crawl.parse_courses(read_fixture("courses.html"), manifest["term_id"])
    by_crn = {c.crn: c for c in courses}
    assert "11179" in by_crn
    c = by_crn["11179"]
    assert c.subject == "PHI"
    assert c.course_number == "201"
    assert c.title == "Introduction to Philosophy"
    assert c.credits == 3.0
    assert c.days == "TR"
    assert c.start_time == "8:00 am"
    assert c.end_time == "9:15 am"
    assert c.instructor_email.endswith("@aus.edu")
    assert c.instructor_name and c.instructor_name != "TBA"


def test_parse_courses_all_rows_have_core_fields(manifest):
    courses = crawl.parse_courses(read_fixture("courses.html"), manifest["term_id"])
    assert courses
    for c in courses:
        assert c.crn
        assert c.subject == "PHI"
        assert c.term_id == manifest["term_id"]


# ── parse_catalog_page ──────────────────────────────────────────────────────


def test_parse_catalog_page_extracts_hours_and_department():
    entries = crawl.parse_catalog_page(read_fixture("catalog.html"))
    by_num = {e.course_number: e for e in entries}
    assert "201" in by_num
    e = by_num["201"]
    assert e.subject == "PHI"
    assert e.credit_hours == 3.0
    assert e.lecture_hours == 3.0
    assert e.department.endswith("Department")
    assert len(e.description) > 20


# ── parse_detail_page (golden regression — guards the #9 refactor) ──────────


def test_parse_detail_matches_golden(fixtures_dir, manifest):
    golden = json.loads((fixtures_dir / "detail_golden.json").read_text())
    term = manifest["term_id"]
    assert golden, "golden baseline should not be empty"
    for crn, expected in golden.items():
        html = read_fixture(f"detail_{crn}.html")
        detail, deps = crawl.parse_detail_page(html, crn, term)
        assert dataclasses.asdict(detail) == expected["detail"], crn
        assert [dataclasses.asdict(d) for d in deps] == expected["deps"], crn


def test_parse_detail_extracts_prerequisite_dependency(manifest):
    # CRN 11179 (PHI 201) requires WRI 102 with a minimum grade of C-.
    detail, deps = crawl.parse_detail_page(
        read_fixture("detail_11179.html"), "11179", manifest["term_id"]
    )
    assert "WRI 102" in detail.prerequisites
    wri = [d for d in deps if d.subject == "WRI" and d.course_number == "102"]
    assert wri and wri[0].dep_type == "prerequisite"
    assert wri[0].minimum_grade == "C-"
