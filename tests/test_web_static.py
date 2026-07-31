import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import web_static


def make_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hi')", encoding="utf-8")
    return dist


def test_missing_dist_returns_503(tmp_path):
    dist = tmp_path / "dist"  # never created
    resp = web_static.spa_response("", dist=dist)
    assert resp.status_code == 503


def test_index_served_for_root(tmp_path):
    dist = make_dist(tmp_path)
    resp = web_static.spa_response("", dist=dist)
    assert resp.status_code == 200


def test_index_served_for_client_route_fallback(tmp_path):
    dist = make_dist(tmp_path)
    resp = web_static.spa_response("settings/integrations", dist=dist)
    assert resp.status_code == 200
    assert Path(resp.path) == dist / "index.html"


def test_real_asset_file_is_served_directly(tmp_path):
    dist = make_dist(tmp_path)
    resp = web_static.spa_response("assets/app.js", dist=dist)
    assert resp.status_code == 200
    assert Path(resp.path) == dist / "assets" / "app.js"
    assert "immutable" in resp.headers["cache-control"]


def test_unknown_api_route_returns_404_json_not_html(tmp_path):
    dist = make_dist(tmp_path)
    resp = web_static.spa_response("api/does-not-exist", dist=dist)
    assert resp.status_code == 404
    assert resp.media_type == "application/json"


def test_path_traversal_falls_back_to_index(tmp_path):
    dist = make_dist(tmp_path)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    resp = web_static.spa_response("../secret.txt", dist=dist)
    # Must not serve the escaped file -- falls back to the SPA shell instead.
    assert Path(resp.path) == dist / "index.html"


def test_dist_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_TRIAGE_WEB_DIST", str(tmp_path / "custom"))
    assert web_static.dist_dir() == (tmp_path / "custom").resolve()
