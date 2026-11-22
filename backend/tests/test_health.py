from fastapi.testclient import TestClient

from danmaku.main import create_app
from test_workbench import connected_probe


def test_v1_health(tmp_path):
    app = create_app(data_dir=tmp_path, connection_probe=connected_probe)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "api_version": "v1"}


def test_foreign_host_rejected(tmp_path):
    app = create_app(data_dir=tmp_path, connection_probe=connected_probe)
    with TestClient(app, base_url="http://outside.invalid") as client:
        assert client.get("/api/v1/health").status_code == 400


def test_workbench_index_revalidates_on_navigation(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text('<script src="/app-synthetic.js"></script>', encoding="utf-8")
    (ui / "app-synthetic.js").write_text("/* synthetic */", encoding="utf-8")
    app = create_app(data_dir=tmp_path / "data", static_dir=ui, connection_probe=connected_probe)
    with TestClient(app, base_url="http://localhost") as client:
        for path in ("/?build=synthetic", "/index.html"):
            response = client.get(path)
            assert response.status_code == 200 and "/app-synthetic.js" in response.text
            assert response.headers["Cache-Control"] == "no-cache"
        assert client.get("/app-synthetic.js").text == "/* synthetic */"

