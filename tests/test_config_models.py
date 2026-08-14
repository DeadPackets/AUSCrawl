from auscrawl import config, models


def test_endpoints_all_hang_off_the_banner9_base():
    assert config.BASE == "https://register.aus.edu/StudentRegistrationSsb/ssb"
    assert len(config.EP) == 14
    for name, url in config.EP.items():
        assert url.startswith(config.BASE + "/"), name


def test_page_size_matches_server_clamp():
    assert config.PAGE_SIZE == 500


def test_browser_headers_are_internally_consistent():
    h = config.BROWSER_HEADERS
    assert "Chrome/" in h["User-Agent"]
    assert h["Sec-Fetch-Mode"] == "cors"
    assert h["Sec-Fetch-Dest"] == "empty"
    assert h["Sec-Fetch-Site"] == "same-origin"
    assert h["X-Requested-With"] == "XMLHttpRequest"
    assert "Chrome" in h["sec-ch-ua"]


def test_retryable_covers_transient_but_not_permanent():
    assert 500 in config.RETRYABLE_STATUS
    assert 429 in config.RETRYABLE_STATUS
    assert 403 in config.RETRYABLE_STATUS
    assert 404 not in config.RETRYABLE_STATUS
    assert 400 not in config.RETRYABLE_STATUS


def test_section_defaults_are_empty_not_none():
    s = models.Section(crn="1", term_id="202710", subject="ACC",
                       course_number="201", title="T")
    assert s.meetings == []
    assert s.instructors == []
    assert s.attributes == []
    assert s.registration_dates == ""


def test_prereq_rule_holds_either_a_course_or_a_test():
    course = models.PrereqRule(seq=0, req_subject="Computer Science",
                               req_course_number="220", min_grade="C-")
    test = models.PrereqRule(seq=1, connector="Or",
                             test_code="SAT Subject Math Level 2", test_score="600")
    assert course.test_code == ""
    assert test.req_subject == ""
