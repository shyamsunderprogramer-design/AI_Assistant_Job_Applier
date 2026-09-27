"""Dice, Glassdoor, ZipRecruiter and Monster alert emails — offline, with markup
shaped like the real alerts in the person's inbox (Sep 2026)."""

from datetime import datetime, timezone

from data_engineering.scraper.alert_parsers import (parse_dice, parse_glassdoor, parse_monster,
                                                    parse_ziprecruiter)

RECEIVED = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

DICE = """<p>Hi, Jordan.</p><p>Check out these recommended jobs based on your profile:</p>
<a href="https://elinks.dice.com/a/sc/AAA">DEVOPS ENGINEER</a>
<p>AaraTechnologies Inc</p><p>Remote or Jersey City, New Jersey, USA</p><p>Posted: 09-24-2026</p>
<a href="https://elinks.dice.com/a/sc/BBB">Senior Cloud AWS Engineer</a>
<p>Arnex Solutions LLC</p><p>Remote</p><p>Posted: 09-23-2026</p>
<a href="https://elinks.dice.com/a/sc/CCC">View All Recommended Jobs</a>
<a href="https://elinks.dice.com/a/sc/DDD">Get Career Advice</a><p>September 17, 2026</p><p>This week...</p>"""


def test_dice_jobs_and_nothing_else():
    jobs = parse_dice(DICE, RECEIVED)
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("DEVOPS ENGINEER", "AaraTechnologies Inc", "Remote or Jersey City, New Jersey, USA"),
        ("Senior Cloud AWS Engineer", "Arnex Solutions LLC", "Remote")]
    assert jobs[0].posted_at.date().isoformat() == "2026-09-24"
    assert jobs[0].url == "https://elinks.dice.com/a/sc/AAA"


GLASSDOOR = """<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&amp;jl=1"><table><tr>
<td>Dell Technologies</td><td>3.7 ★</td><td>Principal Engineer - Enterprise DevOps</td>
<td>Hopkinton, MA</td><td>$183K - $237K</td><td>(</td><td>Employer est.</td><td>)</td><td>21h</td></tr></table></a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102"><div>Jack Henry &amp; Associates</div>
<div>Senior Cloud Security Engineer</div><div>United States</div><div>Easy Apply</div><div>7d</div></div></a>
<a href="https://www.glassdoor.com/Job/newton-ma-devops">See more jobs</a>"""


def test_glassdoor_reads_each_card():
    dell, jack = parse_glassdoor(GLASSDOOR, RECEIVED)
    assert (dell.company, dell.title, dell.location, dell.salary) == (
        "Dell Technologies", "Principal Engineer - Enterprise DevOps", "Hopkinton, MA", "$183K - $237K")
    assert (RECEIVED - dell.posted_at).total_seconds() == 21 * 3600
    assert jack.company == "Jack Henry & Associates" and jack.extras["easy_apply"]
    assert (RECEIVED - jack.posted_at).days == 7
    assert "&amp;" not in dell.url


ZIP = """<p>Here are today's jobs recommended for you:</p>
<a href="https://www.ziprecruiter.com/ekm/AAA">Senior DevOps Engineer</a>
<p>Leidos • Aldie, VA • On-site</p><p>$107K - $195K/yr</p>
<a href="https://www.ziprecruiter.com/ekm/AAA">View Details</a>
<a href="https://www.ziprecruiter.com/km/BBB">Azure Kubernetes Engineer</a>
<p>georgeconsulting • Charleston, SC • Remote</p><p>$54 - $74/hr</p><p>Estimated Pay</p>
<a href="https://www.ziprecruiter.com/km/CCC">1-Click Apply</a>"""


def test_ziprecruiter_splits_the_bullet_line():
    leidos, george = parse_ziprecruiter(ZIP, RECEIVED)
    assert (leidos.company, leidos.location, leidos.salary) == ("Leidos", "Aldie, VA, On-site", "$107K - $195K/yr")
    assert (george.title, george.location) == ("Azure Kubernetes Engineer", "Charleston, SC, Remote")


MONSTER = """<p>Jordan, here are today's jobs</p>
<a href="http://click.monster.com/f/a/AAA">Lead DevOps Engineer</a>
<p>Boston Engineering Corp</p><p>-</p><p>Waltham</p><p>-</p><p>MA</p>
<a href="http://click.monster.com/f/a/BBB">VIEW JOB</a>
<a href="http://click.monster.com/f/a/CCC">VIEW ALL JOBS</a>"""


def test_monster_reads_title_company_city_state():
    [job] = parse_monster(MONSTER, RECEIVED)
    assert (job.title, job.company, job.location) == ("Lead DevOps Engineer", "Boston Engineering Corp", "Waltham, MA")


def test_the_same_job_in_two_emails_gets_one_id():
    a = parse_monster(MONSTER, RECEIVED)[0]
    b = parse_monster(MONSTER.replace("/AAA", "/ZZZ"), RECEIVED)[0]
    assert a.url != b.url and a.external_id == b.external_id


def test_an_email_that_is_not_an_alert_yields_nothing():
    newsletter = '<a href="https://elinks.dice.com/a/sc/X">Read more →</a><p>JOB MARKET TRENDS</p>'
    assert parse_dice(newsletter, RECEIVED) == []
    assert parse_glassdoor("<p>no cards</p>", RECEIVED) == []
