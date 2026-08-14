"""Capture live Banner 9 responses once, so parser tests run offline.

Run:  uv run --project . python tests/capture_banner9.py
"""

import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from auscrawl import config  # noqa: E402

OUT = pathlib.Path(__file__).parent / "fixtures" / "banner9"
TERM = "202710"

# One simple course, one with nested parentheses, one with test-score prereqs,
# one with no prerequisites at all.
COURSES = [("MTH", "203"), ("CMP", "305"), ("ACC", "201"), ("BIO", "103")]

DETAIL_EPS = {
    "prereqs": "prereqs",
    "coreqs": "coreqs",
    "restrictions": "restrictions",
    "course_attributes": "attributes",
    "course_catalog_details": "catalogdetails",
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c = httpx.Client(http2=True, headers=dict(config.BROWSER_HEADERS),
                     timeout=60, follow_redirects=True)
    manifest = []

    r = c.get(config.EP["terms"], params={"searchTerm": "", "offset": 1, "max": 500})
    (OUT / "terms.json").write_bytes(r.content)
    manifest.append(f"terms.json={config.EP['terms']}")

    c.get(config.EP["term_selection"], params={"mode": "search"})
    c.post(config.EP["term_search"], params={"mode": "search"}, data={"term": TERM})
    r = c.get(config.EP["sections"], params={"txt_term": TERM, "pageOffset": 0,
                                             "pageMaxSize": config.PAGE_SIZE})
    (OUT / "sections_202710_p0.json").write_bytes(r.content)
    manifest.append(f"sections_202710_p0.json=term {TERM} offset 0")

    for key, ref in (("ref_subject", "subjects"), ("ref_instructor", "instructors"),
                     ("ref_attribute", "attributes")):
        r = c.get(config.EP[key], params={"searchTerm": "", "term": TERM,
                                          "offset": 1, "max": 5000})
        (OUT / f"ref_{ref}_202710.json").write_bytes(r.content)
        manifest.append(f"ref_{ref}_202710.json={key} term {TERM}")
        time.sleep(0.3)

    c.get(config.EP["term_selection"], params={"mode": "courseSearch"})
    c.post(config.EP["term_search"], params={"mode": "courseSearch"},
           data={"term": TERM})
    r = c.get(config.EP["catalog"], params={"txt_term": TERM, "pageOffset": 0,
                                            "pageMaxSize": config.PAGE_SIZE})
    (OUT / "catalog_202710_p0.json").write_bytes(r.content)
    manifest.append(f"catalog_202710_p0.json=catalog term {TERM} offset 0")

    for subj, num in COURSES:
        for ep_key, tag in DETAIL_EPS.items():
            r = c.post(config.EP[ep_key], data={"term": TERM, "subjectCode": subj,
                                                "courseNumber": num})
            name = f"{tag}_{subj}{num}.html"
            (OUT / name).write_bytes(r.content)
            manifest.append(f"{name}={ep_key} {subj} {num} term {TERM}")
            time.sleep(0.3)

    (OUT / "manifest.txt").write_text("\n".join(manifest) + "\n")
    print(f"captured {len(manifest)} fixtures into {OUT}")


if __name__ == "__main__":
    main()
