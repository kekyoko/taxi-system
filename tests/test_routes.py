from app.main import app


def test_client_facing_me_route_is_published():
    paths = app.openapi()["paths"]
    assert "/v1/me" in paths
    assert "/v1/rides" in paths
