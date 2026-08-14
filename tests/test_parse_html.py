import json

from auscrawl import parse_html
from auscrawl.models import PrereqRule
from tests.conftest import read_b9


# --- prerequisite table ------------------------------------------------------

def test_simple_single_course_prerequisite():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_MTH203.html"))
    assert len(rules) == 1
    r = rules[0]
    assert r.req_subject == "Math"
    assert r.req_course_number == "104"
    assert r.req_level == "Undergraduate"
    assert r.min_grade == "C-"
    assert r.connector == ""
    assert r.open_paren is False and r.close_paren is False


def test_simple_prerequisite_tree_is_a_bare_leaf():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_MTH203.html"))
    assert parse_html.prereq_tree(rules) == {
        "type": "course", "subject": "Math", "course_number": "104",
        "level": "Undergraduate", "min_grade": "C-",
    }


def test_nested_parentheses_are_captured_as_rows():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_CMP305.html"))
    assert len(rules) == 3
    assert rules[0].connector == ""
    assert rules[1].connector == "And" and rules[1].open_paren is True
    assert rules[2].connector == "Or" and rules[2].close_paren is True


def test_nested_parentheses_build_the_right_tree():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_CMP305.html"))
    tree = parse_html.prereq_tree(rules)
    # CMP220 AND (CMP213 OR MTH213)
    assert tree["type"] == "and"
    assert len(tree["operands"]) == 2
    assert tree["operands"][0]["course_number"] == "220"
    inner = tree["operands"][1]
    assert inner["type"] == "or"
    assert {o["subject"] for o in inner["operands"]} == {"Computer Science", "Math"}


def test_test_score_prerequisites_are_captured():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_ACC201.html"))
    tests = [r for r in rules if r.test_code]
    assert tests, "ACC201 has placement-test prerequisites"
    assert any(t.test_code.startswith("SAT") for t in tests)
    assert all(t.test_score for t in tests)
    assert all(t.req_subject == "" for t in tests)


def test_a_flat_or_chain_becomes_one_or_node():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_ACC201.html"))
    tree = parse_html.prereq_tree(rules)
    assert tree["type"] == "or"
    assert len(tree["operands"]) == len(rules)
    assert any(o["type"] == "test" for o in tree["operands"])
    assert any(o["type"] == "course" for o in tree["operands"])


def test_no_prerequisites_yields_no_rules_and_no_tree():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_BIO103.html"))
    assert rules == []
    assert parse_html.prereq_tree(rules) is None
    assert parse_html.prereq_json(rules) == ""


def test_prereq_json_is_valid_json():
    rules = parse_html.parse_prereq_rules(read_b9("prereqs_CMP305.html"))
    assert json.loads(parse_html.prereq_json(rules))["type"] == "and"


def test_rule_label_renders_a_readable_line():
    course = PrereqRule(seq=0, req_subject="Math", req_course_number="104",
                        req_level="Undergraduate", min_grade="C-")
    assert parse_html.rule_label(course) == "Math 104 (Undergraduate, min grade C-)"
    test = PrereqRule(seq=1, test_code="SAT Subject Math Level 2", test_score="600")
    assert parse_html.rule_label(test) == "SAT Subject Math Level 2 >= 600"


# --- other fragments ---------------------------------------------------------

def test_fragment_text_strips_markup_and_the_no_information_boilerplate():
    assert parse_html.fragment_text(read_b9("coreqs_ACC201.html")) == ""
    txt = parse_html.fragment_text(read_b9("restrictions_ACC201.html"))
    assert "Undergraduate" in txt
    assert "<" not in txt


def test_restriction_groups_are_typed_include_or_exclude():
    groups = parse_html.parse_restriction_groups(read_b9("restrictions_ACC201.html"))
    assert len(groups) == 1
    g = groups[0]
    assert g["mode"] == "include"
    assert g["kind"] == "Levels"
    assert g["values"] == ["Undergraduate (UG)"]


def test_multiple_restriction_groups_including_exclusions():
    groups = parse_html.parse_restriction_groups(read_b9("restrictions_BIO103.html"))
    kinds = {g["kind"]: g for g in groups}
    assert set(kinds) == {"Levels", "Colleges", "Majors"}
    assert kinds["Levels"]["mode"] == "include"
    assert kinds["Colleges"]["mode"] == "exclude"
    assert kinds["Majors"]["mode"] == "exclude"
    assert "Biology (BIO)" in kinds["Majors"]["values"]
    assert len(kinds["Majors"]["values"]) == 7


def test_restrictions_json_round_trips():
    raw = parse_html.restrictions_json(read_b9("restrictions_BIO103.html"))
    parsed = json.loads(raw)
    assert {g["kind"] for g in parsed} == {"Levels", "Colleges", "Majors"}


def test_restrictions_json_is_empty_when_there_are_no_groups():
    assert parse_html.restrictions_json(b"<section></section>") == ""


def test_attributes_parse_into_description_and_code_pairs():
    attrs = parse_html.parse_attributes(read_b9("attributes_ACC201.html"))
    assert len(attrs) == 4
    assert ("Actuarial Math Minor_Elective", "AMTN") in attrs
    assert ("MTH Major_Elective", "MTHE") in attrs


def test_catalog_details_yield_levels_grade_modes_and_schedule_types():
    d = parse_html.parse_catalog_details(read_b9("catalogdetails_MTH203.html"))
    assert d["levels"] == "Graduate GR, Post Bachelor PB, Undergraduate UG"
    assert d["grade_modes"] == "Standard Letter S"
    assert d["schedule_types"] == "Lecture L"


def test_catalog_details_do_not_leak_the_title_or_hours_sections():
    d = parse_html.parse_catalog_details(read_b9("catalogdetails_MTH203.html"))
    assert "Calculus" not in d["levels"]
    assert "Credit" not in d["levels"]
    assert "Lecture: 3" not in d["schedule_types"]


def test_catalog_details_on_a_sparse_fragment_do_not_crash():
    assert parse_html.parse_catalog_details(b"<section></section>") == {
        "levels": "", "grade_modes": "", "schedule_types": ""}
