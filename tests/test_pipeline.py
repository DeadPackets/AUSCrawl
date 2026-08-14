import argparse

from auscrawl import models, pipeline


def _t(*ids):
    return [models.Semester(term_id=i, term_name=i) for i in ids]


def opts(**kw):
    base = dict(terms=None, latest=False, resume=False, force=False,
                no_catalog=False, no_details=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_latest_selects_only_the_newest_term():
    got = pipeline.select_terms(_t("202710", "202640", "200520"),
                                opts(latest=True), set())
    assert [s.term_id for s in got] == ["202710"]


def test_explicit_terms_are_honoured_and_filtered_to_real_ones():
    got = pipeline.select_terms(_t("202710", "202640"),
                                opts(terms=["202640", "999999"]), set())
    assert [s.term_id for s in got] == ["202640"]


def test_resume_skips_terms_already_stored():
    got = pipeline.select_terms(_t("202710", "202640"), opts(resume=True), {"202640"})
    assert [s.term_id for s in got] == ["202710"]


def test_force_ignores_what_is_already_stored():
    got = pipeline.select_terms(_t("202710", "202640"),
                                opts(resume=True, force=True), {"202640"})
    assert len(got) == 2


def test_without_resume_every_term_is_crawled():
    got = pipeline.select_terms(_t("202710", "202640"), opts(), {"202640"})
    assert len(got) == 2


def test_pending_versions_dedupes_across_terms_and_skips_done():
    acc = models.CatalogCourse(subject="ACC", course_number="201", title="T",
                               term_effective="202610")
    cmp_ = models.CatalogCourse(subject="CMP", course_number="305", title="T",
                                term_effective="201510")
    pending = pipeline.pending_versions(
        [("202610", [acc, cmp_]), ("202710", [acc])],
        done={("CMP", "305", "201510")})
    assert pending == [("ACC", "201", "202610", "202710", False)]


def test_pending_versions_queries_at_the_newest_term_the_version_was_seen():
    """Banner keys descriptions and attributes by their own term ranges, so the
    fragment queries must use the newest term the version is in effect."""
    old = models.CatalogCourse(subject="ACC", course_number="301", title="T",
                               term_effective="201210")
    new = models.CatalogCourse(subject="ACC", course_number="301", title="T",
                               term_effective="201210", description="Begins a")
    pending = pipeline.pending_versions(
        [("202710", [new]), ("201210", [old]), ("201610", [old])], done=set())
    assert pending == [("ACC", "301", "201210", "202710", True)]


def test_a_version_live_in_the_newest_term_refetches_even_when_done():
    """Banner amends live versions in place, so done only skips frozen history."""
    live = models.CatalogCourse(subject="ACC", course_number="201", title="T",
                                term_effective="202610")
    frozen = models.CatalogCourse(subject="CMP", course_number="305", title="T",
                                  term_effective="201510")
    pending = pipeline.pending_versions(
        [("202510", [frozen]), ("202710", [live])],
        done={("ACC", "201", "202610"), ("CMP", "305", "201510")})
    assert pending == [("ACC", "201", "202610", "202710", False)]


def test_pending_versions_skips_rows_with_no_effective_term():
    courses = [models.CatalogCourse(subject="ACC", course_number="201", title="T",
                                    term_effective="")]
    assert pipeline.pending_versions([("202710", courses)], done=set()) == []


async def test_one_failing_term_does_not_discard_the_rest_of_the_crawl():
    """A 70-minute crawl must not be thrown away because a single term glitched."""
    from auscrawl import fetch

    async def handler(sess, term_id):
        if term_id == "202411":
            raise fetch.EmptyTerm("simulated transient failure")
        return term_id

    ok, failed = await pipeline.run_terms(
        _FakePool(), ["202710", "202411", "202610"], handler)

    assert ok == ["202710", "202610"]
    assert failed == ["202411"]


class _FakePool:
    async def map_terms(self, terms, handler):
        import asyncio
        return await asyncio.gather(*(handler(None, t) for t in terms),
                                    return_exceptions=True)
