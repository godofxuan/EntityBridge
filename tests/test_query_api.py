from html.parser import HTMLParser

from fastapi.testclient import TestClient

from entitybridge.api import create_app
from entitybridge.cli import seed_demo
from entitybridge.store import Store


def test_entity_api_and_directory_use_published_projection_without_loading_full_json(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path / 'query.sqlite'}", tmp_path / "revisions")
    store.initialize()
    revision = seed_demo(store)
    def forbid_payload(*_args, **_kwargs):
        raise AssertionError("Directory and paginated entity API must use the ready query projection")
    monkeypatch.setattr(store, "_payload", forbid_payload)
    client = TestClient(create_app(store, local_demo=True))
    first = client.get("/entities", params={"limit": 1})
    assert first.status_code == 200 and len(first.json()) == 1
    assert first.headers["X-Revision-Id"] == revision
    assert first.headers["X-Total-Count"] == "3"
    assert client.get(f"/entities/{first.json()[0]['entity_id']}").status_code == 200
    second = client.get("/entities", params={"revision": revision, "offset": 1, "limit": 2})
    assert second.status_code == 200 and len(second.json()) == 2
    assert first.json()[0]["entity_id"] not in {item["entity_id"] for item in second.json()}
    assert client.get("/", params={"query": "Northstar"}).status_code == 200


class PageFormAndHeader(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.search_values = {}
        self.header = []
        self.links = []
        self.in_search = self.in_header = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.in_search = "search" in attributes.get("class", "").split()
        elif tag == "input" and self.in_search and attributes.get("name"):
            self.search_values[attributes["name"]] = attributes.get("value", "")
        elif tag == "header":
            self.in_header = True
        elif tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_search = False
        elif tag == "header":
            self.in_header = False

    def handle_data(self, data):
        if self.in_header:
            self.header.append(data)


def changed_history(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'historical-ui.sqlite'}", tmp_path / "revisions")
    store.initialize()
    store.import_records("synthetic", [{"source_key": "a", "name": "OLD HISTORICAL NAME"}])
    old = store.prepare_revision([])["revision_id"]
    store.publish(old, expected_parent=None)
    entity_id = store.entities()[0]["entity_id"]
    store.import_records("synthetic", [{"source_key": "a", "name": "NEW CURRENT NAME"}])
    current = store.prepare_revision([])["revision_id"]
    store.publish(current, expected_parent=old)
    return store, TestClient(create_app(store, local_demo=True)), old, current, entity_id


def test_submitting_search_from_historical_directory_preserves_selected_revision(tmp_path):
    store, client, old, current, entity_id = changed_history(tmp_path)
    page = client.get("/", params={"revision": old})
    form = PageFormAndHeader(page.text)
    # Submit exactly the controls the rendered GET form supplies, as a browser does.
    searched = client.get("/", params={**form.search_values, "query": "OLD HISTORICAL NAME"})
    assert searched.status_code == 200
    assert old[:8] in "".join(PageFormAndHeader(searched.text).header)
    assert f'/entity/{entity_id}?revision={old}' in searched.text
    assert store.current_revision() == current
    store.engine.dispose()


def test_historical_detail_header_and_record_evidence_share_the_selected_revision(tmp_path):
    store, client, old, current, entity_id = changed_history(tmp_path)
    page = client.get(f"/entity/{entity_id}", params={"revision": old})
    assert page.status_code == 200
    header = "".join(PageFormAndHeader(page.text).header)
    assert old[:8] in header and current[:8] not in header
    assert f"当前查看版本：{old[:8]}" in page.text
    assert "OLD HISTORICAL NAME" in page.text and "NEW CURRENT NAME" not in page.text
    assert f"/links?revision={old}" in page.text
    assert store.current_revision() == current
    store.engine.dispose()


def test_retired_identity_destination_link_stays_on_displayed_revision_after_later_publication(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'retired-links.sqlite'}", tmp_path / "revisions")
    store.initialize()
    left, right = store.import_records("synthetic", [
        {"source_key": "a", "name": "ALPHA SYNTHETIC"},
        {"source_key": "b", "name": "BETA SYNTHETIC"},
    ])["records"]
    edge = {"left": left["record_id"], "right": right["record_id"],
            "left_version": left["record_version_id"], "right_version": right["record_version_id"], "score": 1.0}
    merged = store.prepare_revision([edge])["revision_id"]
    store.publish(merged, expected_parent=None)
    retired_id = store.entities()[0]["entity_id"]
    split = store.prepare_revision([])["revision_id"]
    store.publish(split, expected_parent=merged)
    client = TestClient(create_app(store, local_demo=True))
    displayed = client.get(f"/entity/{retired_id}")
    assert displayed.status_code == 200
    destinations = [href for href in PageFormAndHeader(displayed.text).links if href.startswith("/entity/")]
    assert len(destinations) == 2
    recombined = store.prepare_revision([edge])["revision_id"]
    store.publish(recombined, expected_parent=split)
    # Follow exactly the links shown before the newer revision was published.
    for href in destinations:
        clicked = client.get(href)
        assert clicked.status_code == 200
        assert split[:8] in "".join(PageFormAndHeader(clicked.text).header)
        assert f"当前查看版本：{split[:8]}" in clicked.text
        assert "历史身份" in clicked.text
    assert store.current_revision() == recombined
    store.engine.dispose()
