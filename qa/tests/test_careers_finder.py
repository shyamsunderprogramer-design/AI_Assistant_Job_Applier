"""Careers finder: offline tests, nothing leaves the machine.

The finder sweeps hundreds of thousands of company sites, so the decisions
worth pinning down are the ones that send it somewhere or write an answer:
whose board a URL names, which homepage link is the careers link, which of
several same-named companies a domain belongs to, and when a page is not a
careers page at all (a soft 404, a challenge wall, a parked domain, a site that
answers every address with its homepage). Pages come from fixtures; the
finder's fetcher is a dictionary.
"""

from pathlib import Path

import pytest

from data_engineering.companies import careers_detect as cd
from data_engineering.companies.careers_finder import (
    CareersClient, DomainIndex, Finder, Item, Outcome, Page, log_line,
)

FIXTURES = Path(__file__).parent / "fixtures" / "careers"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# -- whose board a URL names ---------------------------------------------------

@pytest.mark.parametrize("url, system, slug", [
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse", "acme"),
    ("https://job-boards.greenhouse.io/acme", "greenhouse", "acme"),
    ("https://boards.greenhouse.io/embed/job_board/js?for=acme", "greenhouse", "acme"),
    ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse", "acme"),
    ("https://jobs.lever.co/acme/0f2c/apply", "lever", "acme"),
    ("https://jobs.eu.lever.co/acme", "lever", "acme"),
    ("https://jobs.ashbyhq.com/acme/embed", "ashby", "acme"),
    ("https://apply.workable.com/acme/j/AB12CD/", "workable", "acme"),
    ("https://acme.applytojob.com/apply/xyz/Engineer", "jazzhr", "acme"),
    ("https://ats.bamboohr.com/careers/89", "bamboohr", "ats"),     # a real company's slug
    ("https://acme.breezy.hr/p/123", "breezy", "acme"),
    ("https://acme.recruitee.com/o/engineer", "recruitee", "acme"),
    ("https://acme.teamtailor.com/jobs/1-engineer", "teamtailor", "acme"),
    ("https://careers.smartrecruiters.com/Acme1", "smartrecruiters", "Acme1"),
    ("https://jobs.jobvite.com/webmd/job/oMgDAfwZ", "jobvite", "webmd"),
    ("https://careers-acme.icims.com/jobs/1234/engineer/job", "icims", "careers-acme"),
    ("https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS/job/USA/123", "workday", "boeing:wd1:EXTERNAL_CAREERS"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/External", "workday", "acme:wd5:External"),
    ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs", "workday", "acme:wd5:External"),
    ("https://wd3.myworkdaysite.com/recruiting/acme/Careers", "workday", "acme:wd3:Careers"),
    ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc-1&ccId=1", "adp", "abc-1"),
    ("https://myjobs.adp.com/acmecareers/cx/job-listing", "adp", "acmecareers"),
    ("https://recruiting.ultipro.com/ACM1000ACME/JobBoard/0b6f3c55-1a2b-4c3d-9e8f-0123456789ab/", "ultipro",
     "ACM1000ACME/0b6f3c55-1a2b-4c3d-9e8f-0123456789ab"),
    ("https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=ABC123&fromClientSide=true", "paycom", "ABC123"),
    ("https://recruiting.paylocity.com/Recruiting/Jobs/All/0b6f3c55-1a2b-4c3d-9e8f-0123456789ab/Acme", "paylocity",
     "0b6f3c55-1a2b-4c3d-9e8f-0123456789ab"),
    ("https://jobs.dayforcehcm.com/en-US/kgs/candidateportal/jobs/14661", "dayforce", "kgs"),
    ("https://acme.taleo.net/careersection/2/jobsearch.ftl?lang=en", "taleo", "acme/2"),
    ("https://tbe.taleo.net/NA9/ats/careers/v2/searchResults?org=ACME&cws=37", "taleo", "ACME"),
    ("https://career5.successfactors.eu/career?company=AcmeP", "successfactors", "AcmeP"),
    ("https://acme.jobs.personio.de/job/123", "personio", "acme"),
    ("https://ats.rippling.com/acme/jobs", "rippling", "acme"),
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9", "oracle", "eeho:CX_1"),
    ("https://www.governmentjobs.com/careers/acmecounty", "neogov", "acmecounty"),
    ("https://sjobs.brassring.com/TGnewUI/Search/Home/Home?partnerid=25678&siteid=5275", "brassring", "25678:5275"),
])
def test_board_urls_name_system_and_slug(url, system, slug):
    hit = cd.detect_url(url)
    assert hit is not None and (hit.system, hit.slug) == (system, slug)
    assert hit.board_url


@pytest.mark.parametrize("url, system", [
    ("https://apply.workable.com/j/AB12CD", "workable"),        # a posting, no account in it
    ("https://grnh.se/abc123", "greenhouse"),                   # short link
    ("https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", "paylocity"),
    ("https://www.usajobs.gov:443/job/886046200", "usajobs"),
])
def test_system_known_without_a_board_name(url, system):
    hit = cd.detect_url(url)
    assert hit.system == system and hit.slug is None


@pytest.mark.parametrize("url", [
    "https://www.bamboohr.com/pricing",            # the vendor's own site
    "https://www.greenhouse.com/blog",
    "https://www.lever.co/",
    "https://www.acme.com/careers?gh_jid=123",     # own site embedding a board: page-level, not URL-level
    "https://www.linkedin.com/company/acme/jobs",
    "not a url",
])
def test_ordinary_urls_name_no_board(url):
    assert cd.detect_url(url) is None


def test_board_urls_match_what_the_scrapers_read():
    """A board found here must be the board the scraper would fetch."""
    from data_engineering.scraper.runner import SCRAPER_TYPES

    samples = {
        "greenhouse": "https://boards.greenhouse.io/acme", "lever": "https://jobs.lever.co/acme",
        "ashby": "https://jobs.ashbyhq.com/acme", "workable": "https://apply.workable.com/acme/",
        "recruitee": "https://acme.recruitee.com/", "teamtailor": "https://acme.teamtailor.com/jobs",
        "bamboohr": "https://acme.bamboohr.com/careers", "breezy": "https://acme.breezy.hr/",
        "jazzhr": "https://acme.applytojob.com/apply", "jobvite": "https://jobs.jobvite.com/acme/jobs",
        "workday": "https://acme.wd5.myworkdayjobs.com/External",
    }
    for system, url in samples.items():
        hit = cd.detect_url(url)
        assert hit.system == system
        assert hit.board_url == SCRAPER_TYPES[system](None).board_url(hit.slug), system


def test_stored_urls_lose_tracking_parameters_but_keep_board_names():
    assert cd.clean_url("https://x.com/a?utm_source=mail&uid=123&token=abc#frag") == "https://x.com/a"
    assert cd.clean_url("https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=K1&session=s") == \
        "https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=K1"


# -- boards named inside a page ------------------------------------------------

def test_greenhouse_embed_script_names_the_board():
    hits = cd.detect_html(fixture("careers_greenhouse_embed.html"))
    assert [(h.system, h.slug) for h in hits] == [("greenhouse", "acmerobotics")]


def test_board_in_escaped_json_is_found():
    hits = cd.detect_html(fixture("careers_next_data.html"))
    assert ("ashby", "hooli") in {(h.system, h.slug) for h in hits}


def test_custom_domain_board_is_known_by_its_fingerprint():
    page = fixture("careers_teamtailor_custom.html")
    assert cd.detect_html(page) == []
    assert cd.fingerprints(page) == ["teamtailor"]


def test_one_board_named_many_times_is_chosen():
    hits = [cd.detect_url("https://jobs.lever.co/acme/1"), cd.detect_url("https://jobs.lever.co/acme/2")]
    assert cd.choose_board(hits, "Acme Inc").slug == "acme"


def test_portfolio_page_of_many_boards_belongs_to_none_of_them():
    hits = cd.detect_html(fixture("portfolio_jobs.html"))
    assert len({h.slug for h in hits}) == 4
    assert cd.choose_board(hits, "Big Fund Ventures", "bigfund.vc") is None


def test_a_board_resembling_the_company_wins_among_several():
    hits = cd.detect_html(fixture("portfolio_jobs.html"))
    assert cd.choose_board(hits, "Databox Inc").slug == "databox"


def test_an_advert_for_another_company_is_not_its_board():
    hits = cd.detect_html(fixture("aggregator_job_with_ads.html"))
    assert [h.slug for h in hits] == ["toptal"]
    assert cd.choose_board(hits, "Fastly", strict=True) is None


def test_job_page_states_the_employer_site():
    urls = cd.hiring_org_urls(fixture("aggregator_job_with_ads.html"))
    assert "https://www.fastly.com/" in urls


# -- links and their scores ------------------------------------------------------

def test_links_carry_text_label_or_image_alt():
    page = ('<base href="https://acme.com/en/"><a href="careers">Careers</a>'
            '<a href="/jobs" aria-label="Open jobs"><svg></svg></a>'
            '<a href="https://careers.acme.com"><img alt="Join our team" src="x.png"></a>'
            '<a href="mailto:hr@acme.com">Email</a><a href="#top">Top</a>'
            '<iframe src="https://acme.bamboohr.com/jobs/embed2.php"></iframe>')
    links = cd.extract_links(page, "https://acme.com/")
    assert [(link.url, link.text, link.kind) for link in links] == [
        ("https://acme.com/en/careers", "Careers", "a"),
        ("https://acme.com/jobs", "Open jobs", "a"),
        ("https://careers.acme.com", "Join our team", "a"),
        ("https://acme.bamboohr.com/jobs/embed2.php", "", "iframe"),
    ]


def test_footer_careers_link_beats_a_blog_post_about_careers():
    links = cd.extract_links(fixture("home_footer_careers.html"), "https://acmerobotics.com/")
    ranked = cd.rank_careers_links(links, "acmerobotics.com")
    assert [c.url for c in ranked] == ["https://acmerobotics.com/company/careers"]


def test_a_link_straight_to_a_board_scores_highest():
    links = [cd.Link("https://acme.com/careers", "Careers"),
             cd.Link("https://jobs.lever.co/acme", "Open roles")]
    ranked = cd.rank_careers_links(links, "acme.com")
    assert ranked[0].hit.system == "lever" and ranked[1].url == "https://acme.com/careers"


@pytest.mark.parametrize("url, text", [
    ("https://acme.com/karriere", "Karriere"),
    ("https://acme.nl/werken-bij", "Werken bij Acme"),
    ("https://jobs.acme.com/", "Jobs"),
    ("https://acme.com/join-us", "Join us"),
    ("https://acme.com/careers", ""),                 # an icon link: the address alone
])
def test_careers_links_in_their_many_forms(url, text):
    assert cd.score_link(cd.Link(url, text), cd.registrable_domain(url)) >= 30


@pytest.mark.parametrize("url, text", [
    ("https://www.linkedin.com/company/acme/jobs", "Jobs"),
    ("https://acme.com/blog/career-advice", "Career advice for graduates"),
    ("https://acme.com/find-a-job", "Find a job"),
    ("https://acme.com/careers.pdf", "Careers brochure"),
    ("https://acme.com/products", "Products"),
])
def test_things_that_are_not_the_careers_page(url, text):
    assert cd.score_link(cd.Link(url, text), "acme.com") < 40


def test_a_job_link_points_back_to_its_careers_page():
    assert cd.careers_root("https://acme.com/careers/engineering/senior-123") == "https://acme.com/careers"
    assert cd.careers_url_from_posting(
        "https://www.coinbase.com/careers/positions/8177619?gh_jid=8177619") == "https://www.coinbase.com/careers"
    assert cd.careers_url_from_posting("https://join.jfrog.com/job/?job=1&gh_jid=1") == "https://join.jfrog.com/"
    assert cd.careers_url_from_posting("https://metrostar.com/job/devops-engineer/?gh_jid=1") is None


# -- pages that are not careers pages -------------------------------------------

def test_soft_404_is_recognised():
    assert cd.is_soft_404(fixture("soft_404.html"))
    assert not cd.is_soft_404(fixture("careers_greenhouse_embed.html"))


def test_challenge_walls_are_named():
    page = fixture("cloudflare_challenge.html")
    assert cd.bot_wall(403, {"cf-ray": "1", "Server": "cloudflare"}, page) == "cloudflare"
    assert cd.bot_wall(403, {}, "<html>nope</html>") == "forbidden"
    assert cd.bot_wall(200, {}, fixture("careers_greenhouse_embed.html")) is None


def test_parked_domain_is_recognised():
    assert cd.is_parked(fixture("parked.html"), "https://acmewidgets.com/")
    assert cd.is_parked("<html></html>", "https://www.hugedomains.com/domain_profile.cfm?d=acme")
    assert not cd.is_parked(fixture("home_footer_careers.html"), "https://acmerobotics.com/")


def test_careers_page_reads_as_one():
    assert cd.careers_signal(fixture("careers_greenhouse_embed.html"), "https://acme.com/company/careers") >= 3
    assert cd.careers_signal(fixture("home_spa.html"), "https://vandelay.com/") < 3


def test_redirect_pages_name_their_target():
    assert cd.meta_refresh('<meta http-equiv="refresh" content="0; url=/careers/">',
                           "https://acme.com/jobs") == "https://acme.com/careers/"
    assert cd.script_redirect('<script>window.location.href = "https://jobs.lever.co/acme";</script>',
                              "https://acme.com/") == "https://jobs.lever.co/acme"
    assert cd.script_redirect("<p>" + "x" * 7000 + "</p><script>location='/a'</script>", "https://a.com/") is None


# -- domains and names --------------------------------------------------------------

def test_registrable_domains():
    assert cd.registrable_domain("https://careers.acme.co.uk/jobs") == "acme.co.uk"
    assert cd.registrable_domain("www.acme.com") == "acme.com"
    assert cd.registrable_domain("acme.github.io") == "acme.github.io"
    assert cd.registrable_domain("192.168.1.1") is None
    assert cd.domain_root("careers.acme.co.uk") == "acme"


def test_list_domains_are_cleaned_or_refused():
    assert cd.clean_domain("www,dvc.com") == "dvc.com"          # the dump's comma typo
    assert cd.clean_domain("https://WWW.Acme.com/about") == "acme.com"
    assert cd.clean_domain("not a domain") is None
    assert cd.clean_domain("x" * 70 + ".com") is None            # over the DNS label limit


def test_name_keys_drop_legal_forms():
    assert cd.name_key("Zagg, Inc.") == "zagg"
    assert cd.name_key("ACME L.L.C.") == "acme"
    assert cd.name_key("The Home Depot") == "home depot"
    assert cd.name_key("Kelly Services") == "kelly services"     # ordinary words stay
    assert cd.normalize_name("Société Générale") == "societe generale"


def test_domain_choice_among_same_named_companies():
    C = cd.DomainCandidate
    assert cd.pick_domain("Technologent", [C("technologent.com", "united states", 354)])[0] == "technologent.com"
    assert cd.pick_domain("Cisco", [C("cisco.com", "united states", 139423),
                                    C("ciscocollect.com", "united states", 55)])[0] == "cisco.com"
    assert cd.pick_domain("Hinge", [C("hingemarketing.com", "united states", 104),
                                    C("hinge.co", "united states", 61)])[0] == "hinge.co"
    assert cd.pick_domain("E-Solutions", [C("e-solutionsinc.com", "united states", 652),
                                          C("esolutionsgroup.com", "united states", 18)])[0] == "e-solutionsinc.com"
    domain, why = cd.pick_domain("Sierra", [C("callsierra.com", "united states", 42),
                                            C("thesierragroup.com", "united states", 38)])
    assert domain is None and "ambiguous" in why


def test_board_names_that_belong_to_the_company():
    assert cd.slug_matches_name("acmecareers", "Acme Inc")
    assert cd.slug_matches_name("kgs", "Koniag Government Services")
    assert cd.slug_matches_name("boeing:wd1:EXTERNAL", "Boeing")
    assert cd.slug_matches_name("fastlyinc", "Fastly", "fastly.com")
    assert not cd.slug_matches_name("toptal", "Fastly")
    assert not cd.slug_matches_name("", "Fastly")


def test_name_index_reads_the_dump_once_and_caches(tmp_path):
    dump = tmp_path / "dump.csv"
    dump.write_text(
        ",name,domain,year founded,industry,size range,locality,country,linkedin url,"
        "current employee estimate,total employee estimate\n"
        "1,acme robotics,acmerobotics.com,2010,x,51 - 200,,united states,,80,100\n"
        "2,sierra,callsierra.com,2010,x,11 - 50,,united states,,40,42\n"
        "3,sierra,thesierragroup.com,2010,x,11 - 50,,united states,,30,38\n",
        encoding="utf-8")
    index = DomainIndex(dump=dump, cache_path=tmp_path / "cache.json")
    index.prepare(["Acme Robotics, Inc.", "Sierra"], say=lambda m: None)
    assert index.lookup("Acme Robotics, Inc.")[0] == "acmerobotics.com"
    assert index.lookup("Sierra")[0] is None
    assert index.lookup("Nobody Co")[0] is None

    dump.unlink()                          # the cache answers on the next run
    again = DomainIndex(dump=dump, cache_path=tmp_path / "cache.json")
    again.prepare(["Acme Robotics, Inc."], say=lambda m: None)
    assert again.lookup("Acme Robotics, Inc.")[0] == "acmerobotics.com"


# -- the finder, with a dictionary for the internet -------------------------------

class FakeWeb:
    """URL -> Page. Anything not listed does not exist (404)."""

    def __init__(self, pages: dict[str, Page]):
        self.pages = {k.rstrip("/"): v for k, v in pages.items()}
        self.asked: list[str] = []

    def __call__(self, url: str, **kw) -> Page:
        self.asked.append(url)
        hit = cd.detect_url(url)
        if hit:                                     # as the real client: never enters a board
            return Page(url=url, final_url=url, stopped_at_ats=hit)
        page = self.pages.get(url.rstrip("/"))
        if page is None:
            return Page(url=url, final_url=url, status=404, html="<title>Not found</title>")
        return page


def ok(url: str, html: str) -> Page:
    return Page(url=url, final_url=url, status=200, html=html)


def test_homepage_link_to_careers_page_with_embedded_board():
    web = FakeWeb({
        "https://acmerobotics.com/": ok("https://acmerobotics.com/", fixture("home_footer_careers.html")),
        "https://acmerobotics.com/company/careers": ok("https://acmerobotics.com/company/careers",
                                                       fixture("careers_greenhouse_embed.html")),
    })
    out = Finder(web).find_on_domain("acmerobotics.com", "Acme Robotics")
    assert out.status == "found"
    assert out.careers_url == "https://acmerobotics.com/company/careers"
    assert (out.hit.system, out.hit.slug) == ("greenhouse", "acmerobotics")
    assert not any("greenhouse" in url for url in web.asked)     # the board itself is never fetched


def test_careers_link_that_redirects_into_workday():
    home = '<html><title>Globex</title><a href="https://careers.globex.com/">Careers</a></html>'
    workday = cd.detect_url("https://globex.wd5.myworkdayjobs.com/Globex_External")
    web = FakeWeb({
        "https://globex.com/": ok("https://globex.com/", home),
        "https://careers.globex.com/": Page(url="https://careers.globex.com/",
                                            final_url="https://globex.wd5.myworkdayjobs.com/Globex_External",
                                            stopped_at_ats=workday),
    })
    out = Finder(web).find_on_domain("globex.com", "Globex")
    assert out.status == "found" and out.careers_url == "https://careers.globex.com/"
    assert out.hit.slug == "globex:wd5:Globex_External"


def test_site_without_links_is_probed_at_the_usual_addresses():
    web = FakeWeb({
        "https://vandelay.com/": ok("https://vandelay.com/", fixture("home_spa.html")),
        "https://vandelay.com/jobs": ok("https://vandelay.com/jobs", fixture("careers_workday_button.html")),
    })
    out = Finder(web).find_on_domain("vandelay.com", "Vandelay")
    assert out.status == "found" and out.method == "common address"
    assert out.hit.system == "workday"
    assert web.asked[:3] == ["https://vandelay.com/", "https://vandelay.com/careers", "https://vandelay.com/jobs"]


def test_site_answering_every_address_with_its_homepage_has_no_careers_page():
    spa = fixture("home_spa.html").replace("<title>Vandelay</title>", "<title>Vandelay careers jobs</title>")
    pages = {f"https://vandelay.com{p}": ok("https://vandelay.com/x", spa)
             for p in ("/", "/careers", "/jobs", "/company/careers", "/about/careers")}
    out = Finder(FakeWeb(pages)).find_on_domain("vandelay.com", "Vandelay")
    assert out.status == "no-careers-page"


def test_unreachable_and_walled_sites_are_recorded_as_such():
    dead = FakeWeb({"https://gone.com/": Page(url="https://gone.com/", error="dns")})
    dead.pages["https://www.gone.com"] = Page(url="https://www.gone.com/", error="dns")
    assert Finder(dead).find_on_domain("gone.com", "Gone").status == "unreachable"

    walled = FakeWeb({"https://walled.com/": Page(url="https://walled.com/", final_url="https://walled.com/",
                                                   status=403, blocked="cloudflare", error="blocked:cloudflare")})
    out = Finder(walled).find_on_domain("walled.com", "Walled")
    assert out.status == "blocked" and "cloudflare" in out.detail

    parked = FakeWeb({"https://acmewidgets.com/": ok("https://acmewidgets.com/", fixture("parked.html"))})
    assert Finder(parked).find_on_domain("acmewidgets.com", "Acme Widgets").status == "parked"


class StubIndex:
    def __init__(self, answers):
        self.answers = answers

    def lookup(self, name):
        domain = self.answers.get(name)
        return (domain, "only match") if domain else (None, "name not in the company lists")


def test_aggregator_link_refused_by_robots_falls_back_to_the_name():
    jooble = "https://jooble.org/away/123?ckey=devops"
    web = FakeWeb({
        jooble: Page(url=jooble, final_url=jooble, error="robots"),
        "https://acmerobotics.com/": ok("https://acmerobotics.com/", fixture("home_footer_careers.html")),
        "https://acmerobotics.com/company/careers": ok("https://acmerobotics.com/company/careers",
                                                       fixture("careers_greenhouse_embed.html")),
    })
    item = Item("postings", "acme robotics", "Acme Robotics", postings=[("jooble", jooble)])
    out = Finder(web, StubIndex({"Acme Robotics": "acmerobotics.com"})).find(item)
    assert out.status == "found" and out.hit.slug == "acmerobotics"
    assert out.domain == "acmerobotics.com" and out.domain_source.startswith("name match")


def test_company_with_no_domain_anywhere_says_why():
    item = Item("postings", "nobody", "Nobody Ltd",
                postings=[("glassdoor-email", "https://www.glassdoor.com/partner/jobListing.htm?x=1")])
    web = FakeWeb({"https://www.glassdoor.com/partner/jobListing.htm?x=1":
                   Page(url="x", final_url="x", error="robots")})
    out = Finder(web, StubIndex({})).find(item)
    assert out.status == "no-domain"
    assert "robots.txt" in out.detail and "not in the company lists" in out.detail


def test_posting_on_a_watched_board_needs_no_request():
    hit = cd.detect_url("https://jobs.lever.co/acme")
    known = Outcome("found", hit.board_url, hit, "posting on a watched lever board", hit.seen_at)

    def no_network(url, **kw):
        raise AssertionError(f"asked the network for {url}")

    out = Finder(no_network).find(Item("postings", "acme", "Acme", known=known))
    assert out is known


def test_log_line_is_one_short_sentence():
    hit = cd.detect_url("https://boards.greenhouse.io/acme")
    line = log_line(Item("postings", "acme", "Acme Corp"),
                    Outcome("found", "https://acme.com/careers", hit))
    assert line == "Acme Corp → https://acme.com/careers (greenhouse)"


# -- the client never enters a hiring system --------------------------------------

class FakeResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


def test_client_stops_at_a_redirect_into_a_board(monkeypatch):
    from data_engineering.scraper.http_client import HttpSettings

    client = CareersClient(HttpSettings(user_agent="test", respect_robots=False,
                                        min_delay_seconds=0, jitter_seconds=0))
    asked = []

    def get(url, **kw):
        asked.append(url)
        if url == "https://acme.com/careers":
            return FakeResponse(302, {"Location": "https://boards.greenhouse.io/acme"})
        raise AssertionError(f"fetched {url}")

    monkeypatch.setattr(client._session, "get", get)
    page = client.fetch("https://acme.com/careers")
    assert page.stopped_at_ats.slug == "acme" and asked == ["https://acme.com/careers"]

    page = client.fetch("https://jobs.lever.co/acme")       # not even as the first URL
    assert page.stopped_at_ats.system == "lever" and len(asked) == 1


def test_client_remembers_a_host_that_does_not_resolve(monkeypatch):
    import requests

    from data_engineering.scraper.http_client import HttpSettings

    client = CareersClient(HttpSettings(user_agent="test", respect_robots=False,
                                        min_delay_seconds=0, jitter_seconds=0))
    calls = []

    def get(url, **kw):
        calls.append(url)
        raise requests.ConnectionError("Failed to resolve 'gone.example' (NameResolutionError)")

    monkeypatch.setattr(client._session, "get", get)
    assert client.fetch("https://gone.example/").error == "dns"
    assert client.fetch("https://gone.example/careers").error == "dns"
    assert len(calls) == 1


def test_found_boards_join_the_daily_fetch_and_retired_ones_stay_retired(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from data_engineering.companies import careers_finder as cf
    from data_engineering.db.models import Company
    from data_engineering.db.session import get_session, init_engine

    init_engine("sqlite:///:memory:")
    with get_session() as session:
        session.add(Company(name="Old Board", slug="oldboard", source="greenhouse", origin="discovery",
                            active=False))
    found = tmp_path / "new_boards.csv"
    found.write_text("source,slug,name,board_url,origin\n"
                     "greenhouse,exampleco,Example Co,https://job-boards.greenhouse.io/exampleco,careers-finder\n"
                     "greenhouse,oldboard,Old Board,https://job-boards.greenhouse.io/oldboard,careers-finder\n",
                     encoding="utf-8")
    monkeypatch.setattr(cf, "NEW_BOARDS_CSV", found)
    monkeypatch.setattr(cf, "BOARDS_CSV", tmp_path / "boards.csv")
    monkeypatch.setattr(cf, "_ENGINE_READY", True)          # the in-memory database above, not the real one

    assert cf.watch_new_boards(SimpleNamespace(database_url="unused")) == 1
    with get_session() as session:
        assert session.query(Company).filter_by(slug="exampleco").one().active is True
        assert session.query(Company).filter_by(slug="oldboard").one().active is False
    assert "exampleco" in (tmp_path / "boards.csv").read_text(encoding="utf-8")
    assert cf.watch_new_boards(SimpleNamespace(database_url="unused")) == 0      # once only
