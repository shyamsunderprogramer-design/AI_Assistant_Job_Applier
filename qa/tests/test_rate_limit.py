"""Rate limiting — no network.

On 23 Sep 2026 Workable answered every request with HTTP 429. Two things went
wrong at once: the client retried each board for about four minutes, one after
another, so the daily run took nine and a half hours; and slug probing read
each refusal as "no board here" and cached it forever. These pin both.
"""

import pytest

from data_engineering.db import session as session_mod
from data_engineering.db.models import ProbeLog
from data_engineering.db.session import get_session, init_engine
from data_engineering.scraper import http_client
from data_engineering.scraper.discovery import CompanyDiscoverer
from data_engineering.scraper.greenhouse import GreenhouseScraper
from data_engineering.scraper.http_client import (
    FetchError,
    HttpSettings,
    PoliteClient,
    RateLimited,
)
from data_engineering.scraper.workable import WorkableScraper


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


def client_answering(statuses, monkeypatch):
    """A PoliteClient whose session hands back `statuses` in order."""
    client = PoliteClient(HttpSettings(user_agent="test", respect_robots=False,
                                       min_delay_seconds=0, jitter_seconds=0,
                                       backoff_base_seconds=0))
    sent = []

    def request(method, url, **kwargs):
        sent.append(url)
        return FakeResponse(statuses[min(len(sent), len(statuses)) - 1])

    monkeypatch.setattr(client._session, "request", request)
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)
    return client, sent


@pytest.fixture
def db():
    init_engine("sqlite:///:memory:")
    yield
    session_mod._engine = None
    session_mod._SessionFactory = None


# -- the client ------------------------------------------------------------

def test_a_host_that_429s_every_retry_raises_rate_limited(monkeypatch):
    client, sent = client_answering([429], monkeypatch)
    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/api/v1/accounts/a/jobs")
    assert len(sent) == client.settings.max_retries + 1


def test_rate_limited_is_still_a_fetch_error(monkeypatch):
    """Every existing `except FetchError` must keep catching it."""
    client, _ = client_answering([429], monkeypatch)
    with pytest.raises(FetchError):
        client.get("https://apply.workable.com/x")


def test_a_cooling_host_is_not_asked_again(monkeypatch):
    client, sent = client_answering([429], monkeypatch)
    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/a")
    before = len(sent)

    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/b")
    assert len(sent) == before  # nothing went out


def test_cooling_one_host_leaves_the_others_alone(monkeypatch):
    client, sent = client_answering([429, 429, 429, 429, 200], monkeypatch)
    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/a")
    assert client.get("https://boards-api.greenhouse.io/v1/boards/x").status_code == 200


def test_the_host_is_asked_again_once_it_cools_off(monkeypatch):
    client, sent = client_answering([429, 429, 429, 429, 200], monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(http_client.time, "monotonic", lambda: now[0])
    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/a")

    now[0] += client.settings.rate_limit_cooldown_seconds + 1
    assert client.get("https://apply.workable.com/b").status_code == 200


def test_server_errors_do_not_trip_the_cool_off(monkeypatch):
    """A 503 is the board having a bad day, not the host refusing us."""
    client, sent = client_answering([503, 503, 503, 503, 200], monkeypatch)
    with pytest.raises(FetchError) as exc:
        client.get("https://apply.workable.com/a")
    assert not isinstance(exc.value, RateLimited)
    assert client.get("https://apply.workable.com/b").status_code == 200


def test_a_429_that_recovers_is_just_a_response(monkeypatch):
    client, _ = client_answering([429, 200], monkeypatch)
    assert client.get("https://apply.workable.com/a").status_code == 200
    assert client.get("https://apply.workable.com/b").status_code == 200


# -- probing ---------------------------------------------------------------

def test_board_exists_lets_a_refusal_through(monkeypatch):
    client, _ = client_answering([429], monkeypatch)
    for scraper in (WorkableScraper(client), GreenhouseScraper(client)):
        with pytest.raises(RateLimited):
            scraper.board_exists("acme")


def test_board_exists_still_says_no_to_a_404(monkeypatch):
    client, _ = client_answering([404], monkeypatch)
    assert GreenhouseScraper(client).board_exists("acme") is False


def test_a_refused_probe_is_never_cached(db):
    class Refusing:
        name = "workable"

        def board_exists(self, slug):
            raise RateLimited("apply.workable.com is rate-limiting us")

        def board_url(self, slug):
            return f"https://apply.workable.com/{slug}"

    CompanyDiscoverer(scrapers={"workable": Refusing()}, strip_suffixes=[]) \
        .discover_from_names(["Acme"], ["workable"])

    with get_session() as s:
        assert s.query(ProbeLog).count() == 0  # asked again next pass


def test_probe_runner_resumes_only_when_nothing_changed():
    from devops.probe_runner import resume_point

    prev = {"names_file": "n.txt", "total": 1000, "sources": ["ashby", "lever"],
            "status": "stopped", "done": 443}
    assert resume_point(prev, "n.txt", 1000, ["lever", "ashby"], 40) == 440
    assert resume_point(prev, "n.txt", 1000, ["lever", "ashby", "jobvite"], 40) == 0   # new system
    assert resume_point(prev, "other.txt", 1000, ["lever", "ashby"], 40) == 0           # new list
    assert resume_point({**prev, "status": "finished"}, "n.txt", 1000, ["ashby", "lever"], 40) == 0
    assert resume_point({}, "n.txt", 1000, ["ashby"], 40) == 0



def test_each_refusal_in_a_row_doubles_the_cool_off(monkeypatch):
    client, sent = client_answering([429], monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(http_client.time, "monotonic", lambda: now[0])
    base = client.settings.rate_limit_cooldown_seconds
    waits = []
    for _ in range(4):
        with pytest.raises(RateLimited):
            client.get("https://apply.workable.com/x")
        waits.append(client._cooling_until["apply.workable.com"] - now[0])
        now[0] += waits[-1] + 1                      # let it cool, then ask again
    assert waits == [base, base * 2, base * 4, base * 8]


def test_one_answer_resets_the_doubling(monkeypatch):
    client, sent = client_answering([429, 429, 429, 429, 200, 429, 429, 429, 429], monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(http_client.time, "monotonic", lambda: now[0])
    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/a")
    now[0] += client.settings.rate_limit_cooldown_seconds + 1
    assert client.get("https://apply.workable.com/b").status_code == 200
    with pytest.raises(RateLimited):
        client.get("https://apply.workable.com/c")
    assert client._cooling_until["apply.workable.com"] - now[0] == client.settings.rate_limit_cooldown_seconds
