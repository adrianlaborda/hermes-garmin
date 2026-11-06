from datetime import date

import httpx
import pytest

from conftest import API_KEY, Seq, make_jwt
from hermes_garmin import endpoints
from hermes_garmin.client import (
    KEY_PAGE, REFRESH_MARGIN_S, TOKEN_PATH, ApiError, AuthError, NotConfiguredError, NotFoundError,
    RateLimitedError, jwt_expiry)

SLEEP = "/sleep-service/sleep/dailySleepSummariesByDate"


def test_token_exchange_sends_api_key_and_uses_bearer(fake, make_client, clock):
    fake.routes[SLEEP] = []
    client = make_client()
    assert client.get(SLEEP, {"startDate": "2026-11-06", "endDate": "2026-11-06"}) == []

    exchange, data = fake.requests
    assert exchange.method == "POST" and exchange.url.path == TOKEN_PATH
    assert exchange.headers["Authorization"] == f"ApiKey {API_KEY}"
    assert exchange.content == b""
    assert data.headers["Authorization"].startswith("Bearer ey")
    assert data.headers["Accept"] == "application/json"
    assert client.token().expires_at == clock.now + 86_400


def test_token_is_reused_then_renewed_before_expiry(fake, make_client, clock):
    client = make_client()
    first = client.token()
    clock.now += 86_400 - REFRESH_MARGIN_S - 60
    assert client.token() is first
    clock.now += 120  # now inside the refresh margin
    assert client.token() is not first
    assert len(fake.calls(TOKEN_PATH)) == 2


def test_token_body_whitespace_and_quotes_are_stripped(fake, make_client, clock):
    jwt = make_jwt(clock.now + 3600)
    fake.routes[TOKEN_PATH] = httpx.Response(200, text=f' "{jwt}"\n')
    assert make_client().token().value == jwt


def test_token_without_exp_gets_a_short_lifetime(fake, make_client, clock):
    fake.routes[TOKEN_PATH] = httpx.Response(200, text=make_jwt(None))
    assert make_client().token().expires_at == clock.now + 3600


def test_jwt_expiry_tolerates_garbage():
    assert jwt_expiry("not-a-jwt") is None
    assert jwt_expiry("a.%%%.c") is None
    assert jwt_expiry(make_jwt(123)) == 123


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_gives_actionable_message_without_secret(fake, make_client, status):
    fake.routes[TOKEN_PATH] = httpx.Response(status, json={"message": f"bad key {API_KEY}"})
    with pytest.raises(AuthError) as err:
        make_client().token()
    message = str(err.value)
    assert KEY_PAGE in message and "GARMIN_PERSONAL_API_KEY" in message
    # Desktop's settings field first, the terminal command as the alternative.
    assert message.index("Settings > Plugins > garmin-connect") < message.index("hermes config set")
    assert API_KEY not in message
    assert len(fake.calls(TOKEN_PATH)) == 1  # never retried


def test_missing_key(make_client):
    with pytest.raises(NotConfiguredError) as err:
        make_client(key=lambda: "  ").get(SLEEP)
    message = str(err.value)
    assert message.index("Settings > Plugins > garmin-connect") < message.index("hermes config set")


def test_revoked_token_is_re_exchanged_once(fake, make_client):
    fake.routes[SLEEP] = Seq([httpx.Response(401), [{"calendarDate": "2026-11-06"}]])
    assert make_client().get(SLEEP) == [{"calendarDate": "2026-11-06"}]
    assert len(fake.calls(TOKEN_PATH)) == 2


def test_persistent_401_is_an_auth_error(fake, make_client):
    fake.routes[SLEEP] = httpx.Response(401)
    with pytest.raises(AuthError):
        make_client().get(SLEEP)
    assert len(fake.calls(SLEEP)) == 2


def test_retries_server_errors_with_backoff(fake, make_client, sleeps):
    fake.routes[SLEEP] = Seq([httpx.Response(503), httpx.Response(502), []])
    assert make_client().get(SLEEP) == []
    assert sleeps == [0.5, 1.0]


def test_honours_retry_after_on_429(fake, make_client, sleeps):
    fake.routes[SLEEP] = Seq([httpx.Response(429, headers={"Retry-After": "2"}), []])
    make_client().get(SLEEP)
    assert sleeps == [2.0]


def test_gives_up_after_retries(fake, make_client, sleeps):
    fake.routes[SLEEP] = httpx.Response(500)
    with pytest.raises(ApiError, match="server error"):
        make_client(retries=2).get(SLEEP)
    assert len(fake.calls(SLEEP)) == 3

    fake.routes[SLEEP] = httpx.Response(429)
    with pytest.raises(RateLimitedError):
        make_client(retries=0).get(SLEEP, ttl=0)


def test_network_errors_are_retried_then_reported(fake, make_client, sleeps):
    def boom(request):
        raise httpx.ConnectError("down", request=request)
    fake.routes[SLEEP] = boom
    with pytest.raises(ApiError, match="Could not reach Garmin Connect"):
        make_client(retries=1).get(SLEEP)
    assert sleeps == [0.5]


def test_error_details_are_redacted(fake, make_client):
    client = make_client()
    token = client.token().value
    fake.routes[SLEEP] = httpx.Response(400, json={"clientMessage": f"range too long for {token}"})
    with pytest.raises(ApiError) as err:
        client.get(SLEEP)
    assert "range too long" in str(err.value) and token not in str(err.value)


def test_not_found_and_no_content(fake, make_client):
    client = make_client()
    with pytest.raises(NotFoundError):
        client.get("/activity-service/activity/1")
    fake.routes["/hrv-service/hrv/2026-11-06"] = httpx.Response(204)
    assert client.get("/hrv-service/hrv/2026-11-06") is None


def test_cache_by_params_and_ttl(fake, make_client, clock):
    fake.routes[SLEEP] = []
    client = make_client(cache_ttl=300)
    client.get(SLEEP, {"startDate": "2026-11-06"})
    client.get(SLEEP, {"startDate": "2026-11-06"})
    client.get(SLEEP, {"startDate": "2026-11-05"})
    assert len(fake.calls(SLEEP)) == 2
    clock.now += 301
    client.get(SLEEP, {"startDate": "2026-11-06"})
    assert len(fake.calls(SLEEP)) == 3


def test_cache_is_per_key(fake, make_client):
    fake.routes[SLEEP] = []
    keys = iter(["key-a", "key-b"])
    current = {"key": next(keys)}
    client = make_client(key=lambda: current["key"])
    client.get(SLEEP)
    current["key"] = next(keys)
    client.get(SLEEP)
    assert len(fake.calls(SLEEP)) == 2
    assert len(fake.calls(TOKEN_PATH)) == 2


def test_ranges_are_chunked_to_the_documented_maximum(fake, make_client):
    fake.routes.update({
        f"/usersummary-service/stats/steps/daily/{s}/{e}": [{"calendarDate": s}]
        for s, e in [("2026-09-08", "2026-10-05"), ("2026-10-06", "2026-11-02"), ("2026-11-03", "2026-11-06")]
    })
    rows = endpoints.steps(make_client(), date(2026, 9, 8), date(2026, 11, 6))
    assert [r["calendarDate"] for r in rows] == ["2026-09-08", "2026-10-06", "2026-11-03"]


def test_query_ranges_and_wrapped_lists(fake, make_client):
    fake.routes["/usersummary-service/v2/dailysummaries"] = lambda req: {
        "data": [{"calendarDate": req.url.params["startDate"]}]}
    rows = endpoints.daily_summaries(make_client(), date(2026, 6, 1), date(2026, 11, 6))
    assert [r["calendarDate"] for r in rows] == ["2026-06-01", "2026-09-29"]  # 120-day windows
    fake.routes["/usersummary-service/stats/steps/daily/2026-10-01/2026-10-28"] = [{"calendarDate": "a"}]
    fake.routes["/usersummary-service/stats/steps/daily/2026-10-29/2026-11-06"] = [{"calendarDate": "b"}]
    rows = endpoints.steps(make_client(), date(2026, 10, 1), date(2026, 11, 6))
    assert [r["calendarDate"] for r in rows] == ["a", "b"]  # Garmin rejects steps windows over 28 days


def test_activity_paging(fake, make_client):
    path = "/activitylist-service/activities/search/activities"
    fake.routes[path] = lambda req: [{"activityId": int(req.url.params["start"]) + i}
                                     for i in range(int(req.url.params["limit"]))]
    rows = endpoints.activities(make_client(), limit=50, activity_type="running")
    assert len(rows) == 50
    params = [dict(r.url.params) for r in fake.calls(path)]
    assert [(p["start"], p["limit"]) for p in params] == [("0", "40"), ("40", "10")]
    assert params[0]["activityType"] == "running" and params[0]["sortOrder"] == "desc"


def test_download_is_streamed_atomically_and_bounded(fake, make_client, tmp_path):
    path = endpoints.activity_file_path(7)
    fake.routes[path] = httpx.Response(200, content=b"PK\x03\x04" + b"x" * 100)
    client = make_client()
    dest = tmp_path / "7.zip"
    assert client.download(path, dest, max_bytes=1000) == 104
    assert dest.read_bytes().startswith(b"PK")
    assert fake.calls(path)[0].headers["Accept"] == "application/x-zip-compressed"

    big = tmp_path / "8.zip"
    with pytest.raises(ApiError, match="larger than"):
        client.download(path, big, max_bytes=50)
    assert not big.exists() and not (tmp_path / "8.zip.part").exists()
