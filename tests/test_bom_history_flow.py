"""Regression coverage for fixed-link BOM revisions through both views."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.bom import parse_rss
from app.db import Database
from app.poller import BomPoller
from app.web.routes import router


class _Radio:
    message_budget = 195
    port = ""
    connected = False
    last_error = ""
    queue_depth = 0

    def status(self):
        return []

    def enqueue(self, *args, **kwargs):
        raise AssertionError("dry-run must not enqueue a radio message")


def _feed(issue_time: str, title_time: str) -> str:
    return f"""<rss><channel><item>
      <title>{title_time} EST Marine Wind Warning Summary for New South Wales</title>
      <link>http://reg.bom.gov.au/nsw/warnings/marinewind.shtml</link>
      <guid>http://reg.bom.gov.au/nsw/warnings/marinewind.shtml</guid>
      <pubDate>{issue_time}</pubDate>
    </item></channel></rss>"""


@pytest.mark.asyncio
async def test_fixed_link_revisions_reach_history_and_dashboard(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "history-flow.db"))
    radio = _Radio()
    poller = BomPoller(db, radio)
    feeds = [
        _feed("Tue, 29 Sep 2026 10:00:00 +0000", "29/20:00"),
        _feed("Tue, 29 Sep 2026 10:00:00 +0000", "29/20:00"),
        _feed("Wed, 30 Sep 2026 06:19:54 +0000", "30/16:19"),
    ]

    class _BOMClient:
        last_errors = []
        last_server_date = None

        async def fetch_active(self, regions, districts=None):
            raw = feeds.pop(0)
            return parse_rss(raw), raw

    async def _enrich(reference):
        return SimpleNamespace(locations="", summary="")

    monkeypatch.setattr("app.poller.BOMClient", _BOMClient)
    monkeypatch.setattr(poller._enricher, "enrich", _enrich)
    monkeypatch.setattr("app.poller.brief_bom_parts", lambda alert, tz, action, budget: ["marine warning"])

    await poller.poll_once()
    await poller.poll_once()
    assert len(db.query_history()) == 1
    await poller.poll_once()
    footers = [row["message"] for row in db.recent_events(100)
               if "[DRY-RUN] would send: UNOFFICIAL relay" in row["message"]]
    assert len(footers) == 1
    rows = db.query_history()
    assert len(rows) == 2
    assert rows[0]["alert_id"] == rows[1]["alert_id"]
    assert rows[0]["revision_hash"] != rows[1]["revision_hash"]
    assert rows[0]["disposition"] == "update"

    app = FastAPI()
    app.include_router(router)
    app.state.db = db
    app.state.tx = radio
    app.state.poller = poller
    client = TestClient(app)
    history = client.get("/history")
    dashboard = client.get("/")
    assert history.status_code == 200
    assert history.text.count('class="rec"') == 2
    assert "Unchanged polls do not add entries" in history.text
    assert dashboard.status_code == 200
    assert "Recent Notices" in dashboard.text
    assert "Locally transmitted by this radio" in dashboard.text
    assert "No notices transmitted yet." in dashboard.text
    assert "Earlier revision" in history.text
    db.close()


@pytest.mark.asyncio
async def test_dry_run_verification_follows_all_alerts_in_poll(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "batch.db"))
    poller = BomPoller(db, _Radio())

    class _BOMClient:
        last_errors = []
        last_server_date = None

        async def fetch_active(self, regions, districts=None):
            return [
                {"id": "one", "event": "Flood Warning", "headline": "one", "area_desc": "Hunter"},
                {"id": "two", "event": "Flood Warning", "headline": "two", "area_desc": "Sydney"},
            ], "<rss/>"

    monkeypatch.setattr("app.poller.BOMClient", _BOMClient)
    await poller.poll_once()
    messages = [row["message"] for row in reversed(db.recent_events(10))]
    assert len(messages) == 3
    assert "Hunter" in messages[0]
    assert "Sydney" in messages[1]
    assert "UNOFFICIAL relay" in messages[2]
    db.close()
