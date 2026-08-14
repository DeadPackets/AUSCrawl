"""Pure parsers for the Banner 9 HTML detail fragments."""

import json
import re

from lxml import html as lxml_html

from .models import PrereqRule

RE_WS = re.compile(r"\s+")
RE_NO_INFO = re.compile(r"No [a-z ]*information available\.?", re.IGNORECASE)
RE_RESTR_HEADER = re.compile(
    r"^(must|cannot|may not)\s+be\s+enrolled\s+in\s+one\s+of\s+the\s+following"
    r"\s+(.+?):\s*$",
    re.IGNORECASE,
)
RE_ATTR = re.compile(r"^(.*\S)\s+([A-Z][A-Z0-9]{1,5})$")
# Banner appends its short code to each value ("Undergraduate UG"); the
# published database has always stored the name alone.
RE_TRAILING_CODE = re.compile(r"^(.*\S)\s+[A-Z][A-Z0-9]{0,3}$")

# Banner separates items with <br/>, but starts a new labelled section with a bold
# span or a div without any <br/> in between — so both need to become line breaks.
RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
RE_SECTION_START = re.compile(r"<(span[^>]*status-bold|div)\b", re.IGNORECASE)


def _text(el) -> str:
    return RE_WS.sub(" ", el.text_content()).strip()


def _lines(raw: str | bytes) -> list[str]:
    if not raw:
        return []
    doc = lxml_html.fromstring(raw)
    for hidden in doc.xpath('//*[contains(@class,"notvisible")]'):
        hidden.drop_tree()
    markup: str = lxml_html.tostring(doc, encoding="unicode")
    markup = RE_BR.sub("\n", markup)
    markup = RE_SECTION_START.sub(r"\n<\1", markup)
    text = lxml_html.fromstring(markup).text_content()
    return [RE_WS.sub(" ", ln).strip() for ln in text.split("\n") if ln.strip()]


# --- prerequisites -----------------------------------------------------------

def parse_prereq_rules(raw: str | bytes) -> list[PrereqRule]:
    """Read the prerequisite table, whose columns are the boolean expression.

    Columns: And/Or | ( | Test | Score | Subject | Course Number | Level | Grade | )
    """
    if not raw:
        return []
    tables = lxml_html.fromstring(raw).xpath('//table[contains(@class,"basePreqTable")]')
    if not tables:
        return []

    rules: list[PrereqRule] = []
    for row in tables[0].xpath(".//tbody/tr"):
        cells = [_text(td) for td in row.xpath("./td")]
        if len(cells) < 9:
            continue
        connector, open_p, test, score, subj, num, level, grade, close_p = cells[:9]
        if not (test or subj):
            continue
        rules.append(PrereqRule(
            seq=len(rules),
            connector=connector,
            open_paren="(" in open_p,
            close_paren=")" in close_p,
            test_code=test,
            test_score=score,
            req_subject=subj,
            req_course_number=num,
            req_level=level,
            min_grade=grade,
        ))
    return rules


def _leaf(r: PrereqRule) -> dict:
    if r.test_code:
        return {"type": "test", "test": r.test_code, "score": r.test_score}
    return {"type": "course", "subject": r.req_subject,
            "course_number": r.req_course_number,
            "level": r.req_level, "min_grade": r.min_grade}


def _combine(op: str, left, right):
    """Flatten same-operator chains so 'a or b or c' is one node, not a spine."""
    if left is None:
        return right
    if left.get("type") == op:
        return {"type": op, "operands": left["operands"] + [right]}
    return {"type": op, "operands": [left, right]}


def prereq_tree(rules: list[PrereqRule]):
    """Fold the rows into a boolean tree, honouring the parenthesis columns."""
    if not rules:
        return None

    stack: list[tuple] = []          # (node so far, operator that will rejoin it)
    node = None
    op = None

    for r in rules:
        if r.connector:
            op = r.connector.lower()
        if r.open_paren:
            stack.append((node, op))
            node, op = None, None

        leaf = _leaf(r)
        node = leaf if node is None else _combine(op or "and", node, leaf)

        if r.close_paren and stack:
            outer, outer_op = stack.pop()
            node = _combine(outer_op or "and", outer, node)
            op = outer_op

    while stack:                      # unbalanced parentheses in the source
        outer, outer_op = stack.pop()
        node = _combine(outer_op or "and", outer, node)

    return node


def prereq_json(rules: list[PrereqRule]) -> str:
    tree = prereq_tree(rules)
    return json.dumps(tree, separators=(",", ":")) if tree else ""


def rule_label(r: PrereqRule) -> str:
    if r.test_code:
        return f"{r.test_code} >= {r.test_score}" if r.test_score else r.test_code
    quals = ", ".join(p for p in (
        r.req_level, f"min grade {r.min_grade}" if r.min_grade else "") if p)
    base = f"{r.req_subject} {r.req_course_number}".strip()
    return f"{base} ({quals})" if quals else base


# --- other fragments ---------------------------------------------------------

def fragment_text(raw: str | bytes) -> str:
    """Readable text of a fragment; the 'no information' filler collapses to ''."""
    if not raw:
        return ""
    doc = lxml_html.fromstring(raw)
    for h in doc.xpath("//h3"):
        h.drop_tree()
    return RE_NO_INFO.sub("", RE_WS.sub(" ", doc.text_content()).strip()).strip()


def parse_restriction_groups(raw: str | bytes) -> list[dict]:
    """Typed groups: each header line opens a group the following lines belong to."""
    groups: list[dict] = []
    current: dict | None = None
    for line in _lines(raw):
        m = RE_RESTR_HEADER.match(line)
        if m:
            current = {
                "mode": "include" if m.group(1).lower() == "must" else "exclude",
                "kind": m.group(2).strip(),
                "values": [],
            }
            groups.append(current)
            continue
        if current is not None and not RE_NO_INFO.match(line) and \
                "Not all restrictions are applicable" not in line:
            current["values"].append(line)
    return [g for g in groups if g["values"]]


def restrictions_json(raw: str | bytes) -> str:
    groups = parse_restriction_groups(raw)
    return json.dumps(groups, separators=(",", ":")) if groups else ""


def parse_attributes(raw: str | bytes) -> list[tuple[str, str]]:
    """'Actuarial Math Minor_Elective AMTN' -> ('Actuarial Math Minor_Elective','AMTN')."""
    out: list[tuple[str, str]] = []
    for line in _lines(raw):
        if RE_NO_INFO.match(line):
            continue
        m = RE_ATTR.match(line)
        if m:
            out.append((m.group(1).strip(), m.group(2)))
    return out


_CATALOG_HEADERS = {
    "Levels:": "levels",
    "Grading Modes:": "grade_modes",
    "Schedule Types:": "schedule_types",
}


def parse_catalog_details(raw: str | bytes) -> dict:
    """Pull the Levels / Grading Modes / Schedule Types sections out of the fragment."""
    buckets: dict[str, list[str]] = {k: [] for k in ("levels", "grade_modes",
                                                     "schedule_types")}
    key: str | None = None
    for line in _lines(raw):
        if line in _CATALOG_HEADERS:
            key = _CATALOG_HEADERS[line]
            continue
        if line.endswith(":"):
            key = None
            continue
        if key:
            buckets[key].append(line)
    return {k: ", ".join(strip_code(x) for x in v) for k, v in buckets.items()}


def strip_code(value: str) -> str:
    m = RE_TRAILING_CODE.match(value)
    return m.group(1) if m else value
