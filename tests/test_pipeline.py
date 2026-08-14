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
    courses = [
        models.CatalogCourse(subject="ACC", course_number="201", title="T",
                             term_effective="202610"),
        models.CatalogCourse(subject="ACC", course_number="201", title="T",
                             term_effective="202610"),   # same version, other term
        models.CatalogCourse(subject="CMP", course_number="305", title="T",
                             term_effective="201510"),
    ]
    pending = pipeline.pending_versions(courses, done={("CMP", "305", "201510")})
    assert pending == [("ACC", "201", "202610")]


def test_pending_versions_skips_rows_with_no_effective_term():
    courses = [models.CatalogCourse(subject="ACC", course_number="201", title="T",
                                    term_effective="")]
    assert pipeline.pending_versions(courses, done=set()) == []


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
