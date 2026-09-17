import unittest

from scripts.continuous_source_ingest import clean_title, gitlab_event, select_new, teams_event


class ContinuousSourceIngestTests(unittest.TestCase):
    def test_clean_title_removes_html(self):
        self.assertEqual(clean_title("<p>Hello&nbsp;World</p>", "x"), "Hello World")

    def test_teams_event_is_stable(self):
        item = {"id": "1789634222885", "body": {"content": "<p>ReqSys Teams</p>"}}
        first = teams_event(item)
        replay = teams_event(item)
        self.assertEqual(first.event_id, replay.event_id)
        self.assertEqual(first.idempotency_key, replay.idempotency_key)

    def test_gitlab_event_is_stable(self):
        item = {"id": "71432e214495a60ca053b9bbd6f72d0180ff612e", "title": "commit real"}
        first = gitlab_event(item)
        replay = gitlab_event(item)
        self.assertEqual(first.event_id, replay.event_id)
        self.assertEqual(first.idempotency_key, replay.idempotency_key)

    def test_select_new_initial_limits_without_backfill(self):
        items = [{"id": "3"}, {"id": "2"}, {"id": "1"}]
        self.assertEqual(select_new(items, [], "id", False), [{"id": "3"}])
        self.assertEqual(select_new(items, ["3"], "id", False), [{"id": "1"}, {"id": "2"}])

    def test_first_sync_seeds_existing_history(self):
        from unittest.mock import patch
        from scripts.continuous_source_ingest import process_source

        state = {"teams": [], "gitlab": []}
        items = [
            {"id": "3", "body": {"content": "newest"}},
            {"id": "2", "body": {"content": "older"}},
            {"id": "1", "body": {"content": "oldest"}},
        ]
        with patch("scripts.continuous_source_ingest.fetch_teams_messages", return_value=items), \
             patch("scripts.continuous_source_ingest.publish_event", return_value={"accepted": True}):
            result = process_source("teams", state, backfill=False)
        self.assertEqual(result["selected"], 1)
        self.assertEqual(state["teams"], ["3", "2", "1"])
