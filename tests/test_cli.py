import pytest

from auscrawl import cli, config


def test_defaults_match_the_documented_behaviour():
    a = cli.build_parser().parse_args([])
    assert a.output == "aus_data.db"
    assert a.rate == config.DEFAULT_RATE
    assert a.latest is False and a.resume is False and a.force is False


def test_output_never_defaults_to_the_shipped_database():
    assert cli.build_parser().parse_args([]).output != "aus_courses.db"


def test_documented_flags_all_parse():
    p = cli.build_parser()
    assert p.parse_args(["--latest"]).latest is True
    assert p.parse_args(["-t", "202620", "202510"]).terms == ["202620", "202510"]
    assert p.parse_args(["--resume"]).resume is True
    assert p.parse_args(["--force"]).force is True
    assert p.parse_args(["--no-catalog", "--no-details"]).no_details is True
    assert p.parse_args(["--rate", "4"]).rate == 4.0
    assert p.parse_args(["-o", "x.db"]).output == "x.db"


def test_rate_above_the_ceiling_is_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--rate", "500"])


def test_rate_of_zero_is_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--rate", "0"])


def test_crawl_shim_still_exposes_main():
    import crawl
    assert callable(crawl.main)
