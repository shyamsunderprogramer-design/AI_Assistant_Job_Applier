"""A long discovery run must not get slower as it goes."""

from data_engineering.scraper.http_client import HttpSettings, PoliteClient


def test_cookies_and_robots_do_not_pile_up():
    client = PoliteClient(HttpSettings(user_agent="test"))
    for n in range(client.MAX_COOKIES + 5):
        client._session.cookies.set(f"c{n}", "1", domain=f"co{n}.example.com")
    client._robots.update({f"https://co{n}.example.com": None for n in range(client.MAX_ROBOTS + 5)})
    client._forget_old()
    assert len(client._session.cookies) == 0 and client._robots == {}
