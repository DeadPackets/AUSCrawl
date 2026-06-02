"""Tests for the coverage-expansion features (G1–G4)."""

import json

import crawl
from conftest import read_fixture


# ── G1: all instructors on co-taught sections ───────────────────────────────


def _sections_by_crn(html, term="202520"):
    out: dict[str, list] = {}
    for c in crawl.parse_courses(html, term):
        out.setdefault(c.crn, []).extend(c.instructors)
    return out


def test_multi_instructor_section_captures_everyone():
    by_crn = _sections_by_crn(read_fixture("courses_multi.html"))
    # CHE 699-03 (CRN 21600) is team-taught by three professors.
    insts = {i.name: i for i in by_crn["21600"]}
    assert set(insts) == {
        "Naif Abdelaziz Darwish",
        "Nabil Mohamed Jabr Abdel Jabbar",
        "Sameer Al-Asheh",
    }
    # Exactly one primary, and it's the (P)-marked one.
    primaries = [n for n, i in insts.items() if i.is_primary]
    assert primaries == ["Naif Abdelaziz Darwish"]
    # Every instructor has a decoded email.
    assert all(i.email.endswith("@aus.edu") for i in insts.values())


def test_primary_fields_stay_backward_compatible():
    courses = crawl.parse_courses(read_fixture("courses_multi.html"), "202520")
    che699 = next(c for c in courses if c.crn == "21600")
    # The flat instructor_name/email keep the primary for existing consumers.
    assert che699.instructor_name == "Naif Abdelaziz Darwish"
    assert che699.instructor_email.endswith("@aus.edu")


def test_single_instructor_still_parsed():
    # PHI fixture: every section has one instructor, marked primary.
    for c in crawl.parse_courses(read_fixture("courses.html"), "202710"):
        if c.instructors:
            assert len(c.instructors) == 1
            assert c.instructors[0].is_primary
            assert c.instructors[0].name == c.instructor_name


def test_bulk_save_populates_section_instructors_and_global_table():
    conn = crawl.init_db(":memory:")
    sem = crawl.Semester(term_id="202520", term_name="Spring 2025")
    courses = crawl.parse_courses(read_fixture("courses_multi.html"), "202520")
    crawl.bulk_save(conn, [sem], [], [(sem, courses)])

    rows = conn.execute(
        "SELECT name, is_primary FROM section_instructors WHERE crn='21600' ORDER BY name"
    ).fetchall()
    assert {r[0] for r in rows} == {
        "Naif Abdelaziz Darwish",
        "Nabil Mohamed Jabr Abdel Jabbar",
        "Sameer Al-Asheh",
    }
    assert sum(r[1] for r in rows) == 1  # exactly one primary

    # A secondary-only instructor still lands in the global instructors table.
    n = conn.execute(
        "SELECT COUNT(*) FROM instructors WHERE name='Sameer Al-Asheh'"
    ).fetchone()[0]
    assert n == 1
    conn.close()


# ── G4: structured restrictions (typed include/exclude groups) ───────────────


def test_restrictions_parsed_into_typed_groups():
    # CRN 11509: allowed Levels = Undergraduate; excluded Classifications.
    detail, _deps = crawl.parse_detail_page(read_fixture("detail_11509.html"), "11509", "202710")
    groups = json.loads(detail.restrictions_json)
    by_type = {g["type"]: g for g in groups}

    assert by_type["Levels"]["include"] is True
    assert by_type["Levels"]["values"] == ["Undergraduate"]

    assert by_type["Classifications"]["include"] is False
    assert by_type["Classifications"]["values"] == [
        "New First-Year", "First-Year I", "First-Year II",
    ]


def test_simple_restrictions_single_group():
    detail, _ = crawl.parse_detail_page(read_fixture("detail_11179.html"), "11179", "202710")
    groups = json.loads(detail.restrictions_json)
    assert groups == [{"include": True, "type": "Levels", "values": ["Undergraduate"]}]


# ── G3: prerequisite boolean logic ──────────────────────────────────────────


def _tree(fixture, crn, term):
    detail, _ = crawl.parse_detail_page(read_fixture(fixture), crn, term)
    return json.loads(detail.prerequisites_json)


def test_single_prerequisite_is_a_bare_course_leaf():
    assert _tree("detail_11179.html", "11179", "202710") == {
        "type": "course", "subject": "WRI", "course_number": "102",
        "min_grade": "C-", "level": "Undergraduate", "concurrent": False,
    }


def test_or_prerequisite():
    tree = _tree("detail_11509.html", "11509", "202710")
    assert tree["type"] == "or"
    assert {(o["subject"], o["course_number"]) for o in tree["operands"]} == {
        ("ENG", "203"), ("ENG", "204"),
    }
    assert all(o["min_grade"] == "C-" for o in tree["operands"])


def test_complex_prerequisite_respects_grouping_and_precedence():
    # "(all of DES 112/121/122/132) AND (one of MTH…) AND (one of WRI…)"
    tree = _tree("detail_complex_10001.html", "10001", "201810")
    assert tree["type"] == "and"
    direct_courses = {(o["subject"], o["course_number"])
                      for o in tree["operands"] if o["type"] == "course"}
    assert {("DES", "112"), ("DES", "121"), ("DES", "122"), ("DES", "132")} <= direct_courses
    or_groups = [o for o in tree["operands"] if o["type"] == "or"]
    assert len(or_groups) == 2
    subjects_per_or = [{o["subject"] for o in g["operands"]} for g in or_groups]
    assert {"MTH"} in subjects_per_or and {"WRI"} in subjects_per_or


def test_concurrent_prerequisite_flag_from_catalog_detail():
    # BIO 101 catalog detail: "BIO 101L Minimum Grade of C- (pre-req concurrent)"
    items, _ = crawl.extract_label_sections(
        _ntdefault(read_fixture("catalog_detail_BIO101.html")),
        ("Prerequisites", "Corequisites", "Restrictions"),
    )
    tree = crawl.requirement_tree(items["Prerequisites"])
    assert tree == {
        "type": "course", "subject": "BIO", "course_number": "101L",
        "min_grade": "C-", "level": "Undergraduate", "concurrent": True,
    }


def test_precedence_and_binds_tighter_than_or_with_parens():
    # Synthetic: "A and ( B or C )" -> and(A, or(B, C))
    from lxml import html as lh
    html = (
        '<td class="ntdefault">'
        '<span class="fieldlabeltext">Prerequisites: </span><br/>'
        ' Undergraduate level <a href="x">MTH 101</a> Minimum Grade of C and ( '
        ' Undergraduate level <a href="x">PHY 101</a> Minimum Grade of D or '
        ' Undergraduate level <a href="x">PHY 102</a> Minimum Grade of D )'
        '</td>'
    )
    cell = lh.fromstring(html)
    items, _ = crawl.extract_label_sections(cell, ("Prerequisites",))
    tree = crawl.requirement_tree(items["Prerequisites"])
    assert tree["type"] == "and"
    assert tree["operands"][0] == {
        "type": "course", "subject": "MTH", "course_number": "101",
        "min_grade": "C", "level": "Undergraduate", "concurrent": False,
    }
    assert tree["operands"][1]["type"] == "or"
    assert {(o["subject"], o["course_number"]) for o in tree["operands"][1]["operands"]} == {
        ("PHY", "101"), ("PHY", "102"),
    }


def _ntdefault(html):
    from lxml import html as lh
    return lh.fromstring(html).xpath('//td[@class="ntdefault"]')[0]


# ── G2: catalog course-detail (richer course-level data) ────────────────────


def test_catalog_detail_phi201():
    cd = crawl.parse_catalog_detail(read_fixture("catalog_detail_PHI201.html"), "PHI", "201")
    assert cd is not None
    assert cd.levels == "Undergraduate"
    assert cd.schedule_types == "Lecture"
    assert cd.course_attributes == "Culture_A Critical Perspective"
    assert "WRI 102" in cd.prerequisites
    assert "Undergraduate" in cd.restrictions


def test_catalog_detail_bio101_has_multiple_attributes_and_concurrent_prereq():
    cd = crawl.parse_catalog_detail(read_fixture("catalog_detail_BIO101.html"), "BIO", "101")
    assert cd is not None
    assert cd.levels == "Post Bachelor, Undergraduate"
    assert cd.course_attributes == "PSY Major_Elective, Natural Sciences Requirement"
    assert "BIO 101L" in cd.prerequisites
    assert "concurrent" in cd.prerequisites.lower()


def test_catalog_detail_returns_none_for_empty_page():
    # A course-not-found page has only the "Return to Previous" links cell.
    empty = '<html><body><table><tr><td class="ntdefault">' \
            '<a href="#">Return to Previous</a></td></tr></table></body></html>'
    assert crawl.parse_catalog_detail(empty, "XXX", "999") is None


# ── persistence: the structured JSON columns actually reach the DB ──────────


def test_save_details_persists_json_columns():
    conn = crawl.init_db(":memory:")
    detail, deps = crawl.parse_detail_page(read_fixture("detail_11509.html"), "11509", "202710")
    crawl.save_details(conn, [detail], deps)
    row = conn.execute(
        "SELECT prerequisites_json, restrictions_json FROM section_details WHERE crn='11509'"
    ).fetchone()
    assert json.loads(row[0])["type"] == "or"
    assert any(g["type"] == "Classifications" for g in json.loads(row[1]))
    conn.close()


def test_save_catalog_detail_round_trip():
    conn = crawl.init_db(":memory:")
    cd = crawl.parse_catalog_detail(read_fixture("catalog_detail_BIO101.html"), "BIO", "101", "202710")
    crawl.save_catalog_detail(conn, [cd])
    row = conn.execute(
        "SELECT course_attributes, prerequisites FROM catalog_detail "
        "WHERE subject='BIO' AND course_number='101'"
    ).fetchone()
    assert row[0] == "PSY Major_Elective, Natural Sciences Requirement"
    assert "BIO 101L" in row[1]
    conn.close()


# ── G3 backfill: text parser must match the HTML parser on the same content ──


def _both_trees(fixture, crn, term):
    detail, _ = crawl.parse_detail_page(read_fixture(fixture), crn, term)
    from_html = json.loads(detail.prerequisites_json)
    from_text = json.loads(crawl.requirement_json_from_text(detail.prerequisites))
    return from_html, from_text


def test_text_backfill_matches_html_parser_or():
    html, text = _both_trees("detail_11509.html", "11509", "202710")
    assert text == html


def test_text_backfill_matches_html_parser_complex_grouping():
    html, text = _both_trees("detail_complex_10001.html", "10001", "201810")
    assert text == html


def test_text_backfill_single_leaf():
    html, text = _both_trees("detail_11179.html", "11179", "202710")
    assert text == html


def test_text_backfill_ignores_non_course_prereqs():
    # Placement-test prereqs have no course code -> no tree, like the HTML parser.
    assert crawl.requirement_json_from_text("Bridge English Placement Test 1.1") == ""


# ── migration + backfill on a pre-G3 database ───────────────────────────────


def test_migration_adds_json_columns_and_backfills():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    # Simulate an OLD section_details table without the *_json columns.
    conn.execute(
        "CREATE TABLE section_details (id INTEGER PRIMARY KEY, crn TEXT, term_id TEXT, "
        "prerequisites TEXT DEFAULT '', corequisites TEXT DEFAULT '', restrictions TEXT DEFAULT '', "
        "waitlist_capacity INT, waitlist_actual INT, waitlist_remaining INT, fees TEXT, "
        "UNIQUE(crn, term_id))"
    )
    conn.execute(
        "INSERT INTO section_details (crn, term_id, prerequisites) VALUES "
        "('99999','201810','Undergraduate level MTH 101 Minimum Grade of C')"
    )
    conn.commit()
    crawl.migrate_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(section_details)")}
    assert {"prerequisites_json", "corequisites_json", "restrictions_json"} <= cols

    n = crawl.backfill_requirement_json(conn)
    assert n == 1
    tree = json.loads(conn.execute(
        "SELECT prerequisites_json FROM section_details WHERE crn='99999'"
    ).fetchone()[0])
    assert tree == {
        "type": "course", "subject": "MTH", "course_number": "101",
        "min_grade": "C", "level": "Undergraduate", "concurrent": False,
    }
    conn.close()
