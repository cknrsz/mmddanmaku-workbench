import sqlite3
from contextlib import closing

import httpx

from danmaku.adapters.credentials import MemoryCredentialStore
from test_workbench import API, connect_session, create_project, open_client


def test_global_settings_commit_and_rollback_as_one_change(tmp_path):
    class Store(MemoryCredentialStore):
        references = []

        def put(self, secret):
            reference = super().put(secret)
            self.references.append(reference)
            return reference

    def verify(request):
        if "synthetic-rejected" in request.headers["cookie"]:
            return httpx.Response(200, json={"code": -101})
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 17001}})

    store = Store()
    path = f"{API}/settings"
    credential = {"sessdata": "synthetic-session", "bili_jct": "synthetic-token"}
    llm = {
        "provider": "openai_compatible", "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "api_key": "synthetic-key", "thinking_enabled": False, "extra_requirements": "合成要求",
    }
    with open_client(tmp_path, credential_store=store, bilibili_transport=httpx.MockTransport(verify)) as client:
        assert client.get(path).status_code == 401
        headers = connect_session(client)
        initial = client.get(path).json()
        assert not initial["bilibili"]["configured"]
        assert initial["llm"]["provider"] == "mock"
        assert client.put(path, json={"sending": {"rate_limit_seconds": 42}}).status_code == 403
        response = client.put(path, headers=headers, json={
            "bilibili": credential, "llm": llm, "sending": {"rate_limit_seconds": 42},
        })
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        saved = response.json()
        assert saved["bilibili"] == {"configured": True, "account_id": "17001", "version": 1}
        assert saved["llm"]["configured"] and saved["llm"]["extra_requirements"] == "合成要求"
        assert saved["sending"]["rate_limit_seconds"] == 42
        assert all(secret not in response.text for secret in [*credential.values(), llm["api_key"]])
        assert client.get(path).json() == saved

        rejected = client.put(path, headers=headers, json={
            "bilibili": {**credential, "sessdata": "synthetic-rejected"}, "sending": {"rate_limit_seconds": 16},
        })
        assert rejected.status_code == 409
        assert len(store.references) == 2
        invalid = client.put(path, headers=headers, json={
            "bilibili": credential, "llm": {**llm, "remove_api_key": True}, "sending": {"rate_limit_seconds": 16},
        })
        assert invalid.status_code == 422 and client.get(path).json() == saved
        assert len(store.references) == 3 and store.get(store.references[-1]) is None
        assert all(store.get(reference) is not None for reference in store.references[:2])

        updated = client.put(path, headers=headers, json={"sending": {"rate_limit_seconds": 60}}).json()
        assert updated["bilibili"] == saved["bilibili"] and updated["llm"] == saved["llm"]
        assert updated["sending"]["rate_limit_seconds"] == 60
    with open_client(tmp_path, credential_store=store) as client:
        connect_session(client)
        assert client.get(path).json() == updated


def test_review_settings_upgrade_from_schema_26(tmp_path):
    path = f"{API}/settings"
    db = tmp_path / "workbench.sqlite3"
    with open_client(tmp_path) as client:
        project = create_project(client, connect_session(client))

    # Version 26 tracked projects and review revisions but had no review settings table.
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DROP TABLE review_settings")
        connection.execute("UPDATE schema_version SET version = 26")

    with open_client(tmp_path) as client:
        headers = connect_session(client)
        assert client.get(f"{API}/projects/{project}").status_code == 200
        assert client.get(path).json()["review"] == {"uncheck_small_by_default": True}
        update = {"review": {"uncheck_small_by_default": False}}
        assert client.put(path, headers=headers, json=update).json()["review"] == update["review"]

    assert db.with_name("workbench.sqlite3.v26.bak").is_file()
    with open_client(tmp_path) as client:
        connect_session(client)
        assert client.get(path).json()["review"] == update["review"]
