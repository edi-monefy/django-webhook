from datetime import date, datetime, timezone

TEST_JOIN_DATE = date(1970, 1, 1)
TEST_LAST_ACTIVE = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
TEST_USER = {
    "id": 1,
    "name": "Dani",
    "email": "dani@doo.com",
    "join_date": "1970-01-01",
    # DjangoJSONEncoder renders a UTC offset as a trailing "Z".
    "last_active": "2000-01-01T12:00:00Z",
}
