"""The 6am run's progress, read off its own log, for the Jobs page."""

from datetime import datetime, timedelta


def test_progress_is_read_from_the_latest_run(monkeypatch, tmp_path):
    from backend.api import app as webapp
    log = tmp_path / "daily_run.log"
    began = datetime.now() - timedelta(minutes=20)
    old = (began - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    line = lambda t, slug: f"{t:%H:%M:%S}  INFO    data_engineering.scraper.runner greenhouse  {slug}   9 seen    1 match    0 new    0 closed"  # noqa: E731
    rows = [f"{old}  === daily starting ===", "00:00:01  INFO    x Run a — scraping 5 companies, 6 at a time",
            line(began, "old1"), f"{began:%Y-%m-%d %H:%M:%S}  === daily starting ===",
            f"{began:%H:%M:%S}  INFO    data_engineering.scraper.runner Run b — scraping 400 companies, 6 at a time"]
    rows += [line(began + timedelta(minutes=i), f"co{i}") for i in range(1, 11)]
    log.write_text("\n".join(rows) + "\n")
    monkeypatch.setattr(webapp, "DAILY_LOG", log)
    p = webapp._daily_progress()
    assert p["total"] == 400 and p["done"] == 10 and p["current"] == "co10"
    assert p["stage"] == "fetching" and p["started"] == f"{began:%H:%M}"
    assert 500 <= p["quiet_for"] <= 700          # last line ten minutes ago


def test_no_log_means_no_progress(monkeypatch, tmp_path):
    from backend.api import app as webapp
    monkeypatch.setattr(webapp, "DAILY_LOG", tmp_path / "missing.log")
    assert webapp._daily_progress() == {}
