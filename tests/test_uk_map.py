"""The home page map: council outlines to SVG, UK IP ranges to councils, and what /api/map says
for a visitor whose council has nobody yet, has some, or can't be told."""
import pytest
from sqlalchemy import select

from app.models import Council, User, UserStatus
from app.services import uk_map

SQ = lambda x0, y0, x1, y1: [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]
GJ = {"type": "FeatureCollection", "features": [
    {"properties": {"LAD26CD": "E09000012", "LAD26NM": "Hackney"},
     "geometry": {"type": "Polygon", "coordinates": SQ(-0.1, 51.5, 0.0, 51.6)}},
    {"properties": {"LAD26CD": "E09000009", "LAD26NM": "Ealing"},
     "geometry": {"type": "MultiPolygon", "coordinates": [SQ(-0.4, 51.5, -0.25, 51.55)]}},
]}
ROWS = [("81.2.69.0", "81.2.69.255", "GB", "51.55", "-0.05"),    # Hackney
        ("81.2.70.0", "81.2.70.255", "GB", "51.55", "-0.05"),    # merges with the above
        ("81.2.71.0", "81.2.71.255", "GB", "51.52", "-0.30"),    # Ealing
        ("81.2.72.0", "81.2.72.255", "FR", "48.85", "2.35"),     # not the UK: dropped
        ("2a00:23c0::", "2a00:23c0:ffff:ffff:ffff:ffff:ffff:ffff", "GB", "51.52", "-0.30")]


@pytest.fixture
def built(db):
    councils = uk_map.councils_from_geojson(GJ)
    uk_map.save_asset(db, "uk_map_svg", uk_map.build_svg(councils).encode())
    uk_map.save_asset(db, "ip_councils", uk_map.build_ip_table(ROWS, councils, vague_share=None))
    db.commit()
    uk_map.reset_cache()
    yield
    uk_map.reset_cache()


def test_svg_has_every_council():
    svg = uk_map.build_svg(uk_map.councils_from_geojson(GJ))
    assert svg.startswith("<svg") and 'data-gss="E09000012" data-name="Hackney"' in svg and "Ealing" in svg


def test_ip_ranges_map_to_councils():
    councils = uk_map.councils_from_geojson(GJ)
    t = uk_map.IpTable(uk_map.build_ip_table(ROWS, councils, vague_share=None))
    assert len(t.s4) == 2  # the two Hackney ranges merged, France dropped
    assert t.lookup("81.2.69.7") == t.lookup("81.2.70.200") == "E09000012"
    assert t.lookup("81.2.71.1") == "E09000009"
    assert t.lookup("2a00:23c0:12::1") == "E09000009"
    assert t.lookup("81.2.72.1") is None and t.lookup("8.8.8.8") is None and t.lookup("nonsense") is None


def test_a_council_nobody_has_joined_yet(client, db, built, monkeypatch):
    monkeypatch.setattr("app.api.routes.client_ip", lambda request: "81.2.71.9")  # Ealing
    d = client.get("/api/map").json()
    assert d["you"] == {"gss": "E09000009", "name": "Ealing", "count": 0, "rank": None}
    assert client.get("/api/map.svg").headers["content-type"].startswith("image/svg+xml")


def test_a_council_with_residents_and_the_top_10(client, db, built, monkeypatch):
    uid = client.post("/api/signup/start", json={"postcode": "E8 3PB", "uprn": "10000002"}).json()["user_id"]
    u = db.get(User, uid)
    u.status = UserStatus.active
    db.commit()
    uk_map.reset_cache()
    gss = db.scalar(select(Council.gss_code))
    monkeypatch.setattr("app.api.routes.client_ip", lambda request: "81.2.69.9")  # Hackney
    d = client.get("/api/map").json()
    assert d["counts"] == {gss: 1} and d["top"][0]["count"] == 1
    if gss == "E09000012":
        assert d["you"]["rank"] == 1 and d["you"]["count"] == 1


def test_outside_the_uk_or_before_the_map_is_built(client, db, monkeypatch):
    monkeypatch.setattr("app.api.routes.client_ip", lambda request: "8.8.8.8")
    assert client.get("/api/map").json()["you"] is None
    assert client.get("/api/map.svg").status_code == 404


def test_a_location_covering_much_of_the_uk_is_left_out():
    """Half of all UK addresses on one spot in Westminster is the table not knowing, not a place."""
    councils = uk_map.councils_from_geojson(GJ)
    rows = [("10.0.0.0", "10.255.255.255", "GB", "51.55", "-0.05")]  # 16m addresses on one point
    rows += [(f"81.2.{i}.0", f"81.2.{i}.255", "GB", f"51.5{i % 4}", "-0.3{0}".format(i % 5)) for i in range(60)]
    report = []
    t = uk_map.IpTable(uk_map.build_ip_table(rows, councils, report=report))
    assert t.lookup("10.1.2.3") is None
    assert report and report[0][:2] == ("51.55", "-0.05")
    assert t.lookup("81.2.5.1") == "E09000009"  # the specific ones still work


def test_a_place_name_spread_over_many_councils_is_left_out():
    """DB-IP's plain "London" is the city, not the borough: its points scatter over boroughs,
    so they mustn't name one. A named district ("Camberwell") still counts."""
    gj = {"type": "FeatureCollection", "features": [
        {"properties": {"LAD26CD": f"E0900000{i}", "LAD26NM": f"B{i}"},
         "geometry": {"type": "Polygon", "coordinates": SQ(i / 10, 51.0, i / 10 + 0.1, 51.1)}}
        for i in range(1, 4)]}
    councils = uk_map.councils_from_geojson(gj)
    rows = [("81.2.1.0", "81.2.1.255", "GB", "51.05", "0.15", "London"),
            ("81.2.2.0", "81.2.2.255", "GB", "51.05", "0.25", "London"),
            ("81.2.3.0", "81.2.3.255", "GB", "51.05", "0.35", "London"),
            ("81.2.4.0", "81.2.4.255", "GB", "51.06", "0.15", "Camberwell"),
            ("81.2.5.0", "81.2.5.255", "GB", "51.06", "0.25", "Sometown"),
            ("81.2.6.0", "81.2.6.255", "GB", "51.07", "0.25", "Sometown")]
    report = []
    t = uk_map.IpTable(uk_map.build_ip_table(rows, councils, vague_share=None, report=report))
    assert t.lookup("81.2.1.9") is None and t.lookup("81.2.3.9") is None
    assert t.lookup("81.2.4.9") == "E09000001"
    assert t.lookup("81.2.6.9") == "E09000002"  # two points, one council: fine
    assert ("London", 3) in report
