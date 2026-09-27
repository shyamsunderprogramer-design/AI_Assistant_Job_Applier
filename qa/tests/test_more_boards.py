"""Breezy HR, Jobvite and JazzHR boards, and the We Work Remotely and Working
Nomads feeds — offline, with markup copied from the live pages (Sep 2026:
Netrix Global, WebMD, Harris Associates)."""

from data_engineering.scraper.base import CompanyRef
from data_engineering.scraper.breezy import BreezyScraper
from data_engineering.scraper.feeds import weworkremotely, workingnomads
from data_engineering.scraper.jazzhr import JazzHRScraper
from data_engineering.scraper.jazzhr import parse_description as jazz_description
from data_engineering.scraper.jazzhr import parse_list as jazz_list
from data_engineering.scraper.jobvite import parse_description as jv_description
from data_engineering.scraper.jobvite import parse_list as jv_list
from data_engineering.scraper.jobvite import parse_locations
from data_engineering.scraper.runner import SCRAPER_TYPES


class Resp:
    def __init__(self, status=200, body=None, text="", ctype="application/json", url=""):
        self.status_code, self._body, self.text, self.url = status, body, text, url
        self.headers = {"content-type": ctype}

    def json(self):
        return self._body


class Client:
    def __init__(self, answer):
        self.answer = answer

    def get(self, url, **kw):
        return self.answer

    def get_json(self, url, **kw):
        return self.answer.json()


def test_the_three_boards_are_registered():
    assert {"breezy", "jazzhr", "jobvite"} <= set(SCRAPER_TYPES)


# -- Breezy -------------------------------------------------------------------

BREEZY = [{"id": "f3c8fdbe870e", "name": "Senior DevOps Engineer",
           "url": "https://netrix-global.breezy.hr/p/f3c8fdbe870e-senior-devops",
           "published_date": "2026-09-07T13:21:03.048Z", "company": {"name": "Netrix Global"},
           "location": {"country": {"name": "United States", "id": "US"}, "city": "Chicago",
                        "state": {"name": "IL"}, "is_remote": True},
           "description": "<p>Terraform and AWS</p>"}]


def test_breezy_reads_one_verbose_call():
    s = BreezyScraper(Client(Resp(body=BREEZY)))
    [job] = s.fetch_jobs(CompanyRef("Netrix", "netrix-global", "breezy"))
    assert job.location == "Remote (Chicago, IL, United States)"
    assert job.description == "Terraform and AWS" and job.posted_at.day == 7


def test_breezy_not_found_page_is_not_a_board():
    s = BreezyScraper(Client(Resp(404, text="Career portal not found", ctype="text/html")))
    assert not s.board_exists("nobody")


# -- Jobvite ------------------------------------------------------------------

JV_LIST = """<table class="jv-job-list"><tr><td class="jv-job-list-name">
<a href="/webmd/job/oQAqAfwa">Site Reliability Engineer</a></td>
<td class="jv-job-list-location">Atlanta, Georgia</td></tr>
<tr><td class="jv-job-list-name"><a href="/webmd/job/oT8Vzfwf">Platform Engineer</a></td>
<td class="jv-job-list-location">2 Locations</td></tr></table>"""

JV_JOB = """<p class="jv-job-detail-meta"> Engineering<span class='jv-inline-separator'></span>
Atlanta, Georgia <span class="jv-inline-separator"></span> Newark, New Jersey </p>
<div class="jv-job-detail-description" ng-non-bindable><h3>Description</h3><p>Run Kubernetes</p></div>
<div class="jv-job-detail-bottom-actions">Apply</div>"""


def test_jobvite_list_page():
    jobs = jv_list(JV_LIST, CompanyRef("WebMD", "webmd", "jobvite"))
    assert [(j.external_id, j.title, j.location) for j in jobs] == [
        ("oQAqAfwa", "Site Reliability Engineer", "Atlanta, Georgia"),
        ("oT8Vzfwf", "Platform Engineer", "2 Locations")]
    assert jobs[0].application_url == "https://jobs.jobvite.com/webmd/job/oQAqAfwa"


def test_jobvite_job_page_gives_description_and_real_locations():
    assert "Run Kubernetes" in jv_description(JV_JOB)
    assert parse_locations(JV_JOB) == "Atlanta, Georgia; Newark, New Jersey"


# -- JazzHR -------------------------------------------------------------------

JAZZ_LIST = """<li class="list-group-item"> <h3 class='list-group-item-heading'>
<a href="https://harrisassociates.applytojob.com/apply/KfXRsMeXzY/DevOps-Engineer"> DevOps Engineer </a>
</h3> <ul class='list-inline list-group-item-text'> <li><i class='fa fa-map-marker'></i>San Diego, CA</li>
<li><i class='fa fa-sitemap'></i>ENGINEERING</li> </ul> </li>"""

JAZZ_JOB = """<div class="job-header">..</div><div id="job-description">
<p>Automate everything with Ansible</p></div><div class="job-application-form"><form>..</form></div>"""


def test_jazzhr_list_and_posting():
    [job] = jazz_list(JAZZ_LIST, CompanyRef("Harris", "harrisassociates", "jazzhr"))
    assert (job.external_id, job.title, job.location) == ("KfXRsMeXzY", "DevOps Engineer", "San Diego, CA")
    assert jazz_description(JAZZ_JOB) == "Automate everything with Ansible"


def test_jazzhr_marketing_page_is_not_a_board():
    s = JazzHRScraper(Client(Resp(text="<title>JazzHR | Recruiting Software</title>",
                                  ctype="text/html", url="https://www.jazzhr.com/")))
    assert not s.board_exists("nobody")


# -- the two feeds ------------------------------------------------------------

WWR = """<rss version="2.0"><channel><item>
<title>Acme Robotics: Senior DevOps Engineer</title><region>USA Only</region>
<link>https://weworkremotely.com/remote-jobs/acme-senior-devops</link>
<guid>https://weworkremotely.com/remote-jobs/acme-senior-devops</guid>
<pubDate>Fri, 25 Sep 2026 10:00:00 +0000</pubDate><description>&lt;p&gt;Terraform&lt;/p&gt;</description>
</item></channel></rss>"""


def test_we_work_remotely_splits_company_from_title():
    [job] = weworkremotely.parse(WWR)
    assert (job.raw.company, job.raw.title) == ("Acme Robotics", "Senior DevOps Engineer")
    assert job.raw.location == "Remote (USA Only)" and job.raw.description == "Terraform"


def test_working_nomads():
    class H:
        calls = 0
        def get_json(self, url, params=None, headers=None):
            return [{"url": "https://www.workingnomads.com/jobs/sre-acme", "title": "SRE",
                     "company_name": "Acme", "location": "USA", "description": "<p>x</p>",
                     "pub_date": "2026-09-25T10:00:00-04:00"}]
    [job] = workingnomads.fetch(H(), [], None)
    assert job.raw.location == "Remote (USA)" and job.raw.posted_at.hour == 10
