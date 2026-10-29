from datetime import UTC, datetime, timedelta
from uuid import UUID

from posthog.test.base import APIBaseTest
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from parameterized import parameterized
from rest_framework import status

from posthog.hogql.database.models import SavedQuery

from posthog.models.team import Team

from products.data_catalog.backend.facade.contracts import (
    AdjustedRun,
    ChartSource,
    MetricRunCounts,
    ProposedMetric,
    TableSource,
    TopicDefinition,
    UncoveredGroup,
    UsageSnapshotPayload,
)
from products.data_catalog.backend.facade.enums import JudgeRelation, ShapeOutcome, UsageSnapshotStatus
from products.data_catalog.backend.logic.metrics import upsert_metric
from products.data_catalog.backend.logic.usage_snapshots import (
    UsageReader,
    _denied_tables_and_views,
    _viewable_charts,
    payload_to_json,
    visible_payload,
)
from products.data_catalog.backend.models import UsageSnapshot

_SNAPSHOTS = "products.data_catalog.backend.logic.usage_snapshots"
_FLAG = "products.data_catalog.backend.logic.usage_snapshots.posthoganalytics.feature_enabled"
_VIEWABLE = "products.data_catalog.backend.logic.usage_snapshots.viewable_insight_definitions"
_WINDOW_END = datetime(2026, 10, 26, tzinfo=UTC)


def _adjusted(metric_id: UUID, *, is_scout: bool = False, sql: str = "select count() from events") -> AdjustedRun:
    return AdjustedRun(
        metric_id=metric_id,
        read_metric_first=False,
        relation=JudgeRelation.SERVES,
        intent="weekly signups",
        sql=sql,
        reasoning="Same event and aggregate.",
        occurred_at=_WINDOW_END - timedelta(days=1),
        session_id=None,
        is_scout=is_scout,
    )


_VISIBLE = UUID("00000000-0000-0000-0000-000000000001")
_HIDDEN = UUID("00000000-0000-0000-0000-000000000002")


def _group(sql: str, matched: UUID | None = None) -> UncoveredGroup:
    return UncoveredGroup(
        fingerprint_digest=sql,
        measure=sql,
        outcome=ShapeOutcome.SLICE if matched else ShapeOutcome.UNCOVERED,
        matched_metric_id=matched,
        distinct_people=2,
        built=1,
        sample_sql=sql,
        draft_name=None,
        draft_description=None,
    )


def _proposal(definition: str, charts: list[ChartSource] | None = None) -> ProposedMetric:
    return ProposedMetric(
        name="revenue",
        description="Revenue.",
        definition=definition,
        measures=1,
        people=2,
        built=1,
        charts=charts or [],
        other_charts=0,
        surfaces=[],
        agents=None,
    )


def _counts(metric_id: UUID) -> MetricRunCounts:
    return MetricRunCounts(metric_id=metric_id, direct_runs={"web": 1}, direct_people=1, scout_runs=0)


class TestVisiblePayload(SimpleTestCase):
    @parameterized.expand(
        [
            ("no_denied_tables_keeps_every_query", frozenset(), "select sum(amount) from billing", True),
            ("query_over_a_denied_table", frozenset({"billing"}), "select sum(amount) from billing", False),
            ("query_over_another_table", frozenset({"billing"}), "select count() from events", True),
            ("unparseable_query_with_denied_tables", frozenset({"billing"}), "select count( from", False),
            (
                "cut_query_with_denied_tables",
                frozenset({"billing"}),
                "select count() from events where " + "x = 1 and " * 600 + "1",
                False,
            ),
        ]
    )
    def test_reader_sees_only_visible_metrics_and_allowed_sql(
        self, _name: str, denied: frozenset[str], sql: str, kept: bool
    ) -> None:
        payload = UsageSnapshotPayload(
            metric_runs=[_counts(_VISIBLE), _counts(_HIDDEN)],
            adjusted_runs=[_adjusted(_VISIBLE, sql=sql), _adjusted(_HIDDEN)],
        )

        visible = visible_payload(payload, UsageReader(visible_metric_ids=frozenset({_VISIBLE}), denied_tables=denied))

        assert [counts.metric_id for counts in visible.metric_runs or []] == [_VISIBLE]
        assert [run.metric_id for run in visible.adjusted_runs or []] == ([_VISIBLE] if kept else [])

    def test_reader_does_not_see_a_source_over_a_denied_table(self) -> None:
        payload = UsageSnapshotPayload(
            table_sources=[
                TableSource(name="billing", sessions=3, people=2),
                TableSource(name="revenue_daily", sessions=2, people=1),
            ]
        )

        visible = visible_payload(
            payload, UsageReader(visible_metric_ids=frozenset(), denied_tables=frozenset({"billing"}))
        )

        assert [source.name for source in visible.table_sources or []] == ["revenue_daily"]

    @parameterized.expand([("open_reader", frozenset(), 1), ("reader_with_a_denied_table", frozenset({"billing"}), 0)])
    def test_topic_titles_reach_only_readers_without_denied_tables(
        self, _name: str, denied: frozenset[str], kept: int
    ) -> None:
        payload = UsageSnapshotPayload(topic_definitions=[TopicDefinition(title="Payroll", description="Salaries.")])

        visible = visible_payload(payload, UsageReader(visible_metric_ids=frozenset(), denied_tables=denied))

        assert len(visible.topic_definitions or []) == kept

    def test_reader_does_not_see_measures_or_proposals_over_denied_tables_or_hidden_metrics(self) -> None:
        payload = UsageSnapshotPayload(
            uncovered=[
                _group("select sum(amount) from billing"),
                _group("select count() from events", matched=_HIDDEN),
                _group("select count() from events", matched=_VISIBLE),
                _group("select count() from events where " + "x = 1 and " * 200 + "1", matched=_VISIBLE),
            ],
            proposed_metrics=[_proposal("select sum(amount) from billing"), _proposal("select count() from events")],
        )

        visible = visible_payload(
            payload, UsageReader(visible_metric_ids=frozenset({_VISIBLE}), denied_tables=frozenset({"billing"}))
        )

        assert [group.matched_metric_id for group in visible.uncovered or []] == [_VISIBLE]
        assert [proposal.definition for proposal in visible.proposed_metrics or []] == ["select count() from events"]


class TestViewableCharts(SimpleTestCase):
    @parameterized.expand(
        [
            ("no_dashboard_access", False, set(), None),
            ("dashboard_hidden_by_its_own_rule", True, set(), None),
            ("dashboard_the_reader_may_open", True, {7}, "Growth"),
        ]
    )
    def test_reader_sees_only_charts_and_dashboards_they_may_open(
        self, _name: str, sees_dashboards: bool, viewable_dashboards: set[int], dashboard_name: str | None
    ) -> None:
        access = MagicMock()
        access.check_access_level_for_resource.return_value = sees_dashboards
        charts = [
            ChartSource(
                insight_short_id=short_id, insight_name=short_id, dashboard_id=7, dashboard_name="Growth", people=3
            )
            for short_id in ("allowed", "blocked")
        ]
        reader = UsageReader(
            visible_metric_ids=frozenset(),
            denied_tables=frozenset(),
            team=MagicMock(),
            user=MagicMock(),
            user_access_control=access,
        )

        with (
            patch(_VIEWABLE, return_value={"allowed": MagicMock()}),
            patch(f"{_SNAPSHOTS}.viewable_dashboard_ids", return_value=viewable_dashboards),
        ):
            visible = _viewable_charts(
                UsageSnapshotPayload(proposed_metrics=[_proposal("select count() from events", charts)]), reader
            )

        assert [
            (chart.insight_short_id, chart.dashboard_name) for chart in (visible.proposed_metrics or [])[0].charts
        ] == [("allowed", dashboard_name)]


class TestDeniedViews(SimpleTestCase):
    def test_a_view_over_a_denied_table_is_denied_directly_or_through_another_view(self) -> None:
        views = {
            "revenue_view": "select sum(amount) from billing",
            "revenue_rollup": "select * from revenue_view",
            "events_view": "select count() from events",
        }
        database = MagicMock(_denied_tables={"billing"})
        database.get_view_names.return_value = list(views)
        database.get_table.side_effect = lambda name: SavedQuery(id=name, name=name, query=views[name], fields={})

        with patch(f"{_SNAPSHOTS}.Database.create_for", return_value=database):
            denied = _denied_tables_and_views(MagicMock(), MagicMock(), MagicMock())

        assert denied == {"billing", "revenue_view", "revenue_rollup"}


class TestUsageSnapshotAPI(APIBaseTest):
    def _snapshot(
        self,
        *,
        team: Team | None = None,
        snapshot_status: UsageSnapshotStatus = UsageSnapshotStatus.DONE,
        computed_at: datetime | None = _WINDOW_END,
        payload: UsageSnapshotPayload | None = None,
    ) -> UsageSnapshot:
        team = team or self.team
        return UsageSnapshot.objects.for_team(team.id).create(
            team=team,
            status=snapshot_status.value,
            window_start=_WINDOW_END - timedelta(days=28),
            window_end=_WINDOW_END,
            computed_at=computed_at,
            payload=payload_to_json(payload or UsageSnapshotPayload()),
        )

    @parameterized.expand([("latest", "data_catalog/usage/latest/"), ("metric", "data_catalog/metrics/mrr/usage/")])
    def test_usage_endpoints_404_while_flag_is_off(self, _name: str, path: str) -> None:
        upsert_metric(team=self.team, user=self.user, name="mrr", description="Monthly revenue")
        self._snapshot()
        with patch(_FLAG, return_value=False):
            response = self.client.get(f"/api/projects/{self.team.id}/{path}")
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_latest_returns_newest_finished_snapshot_of_the_team(self) -> None:
        url = f"/api/projects/{self.team.id}/data_catalog/usage/latest/"
        with patch(_FLAG, return_value=True):
            assert self.client.get(url).status_code == status.HTTP_404_NOT_FOUND

            self._snapshot(computed_at=_WINDOW_END - timedelta(days=7))
            newest = self._snapshot()
            self._snapshot(snapshot_status=UsageSnapshotStatus.COMPUTING, computed_at=None)
            self._snapshot(snapshot_status=UsageSnapshotStatus.ERROR, computed_at=_WINDOW_END + timedelta(days=1))
            other_team = Team.objects.create(organization=self.organization, name="Other")
            self._snapshot(team=other_team, computed_at=_WINDOW_END + timedelta(days=2))

            response = self.client.get(url)

        assert response.status_code == status.HTTP_200_OK, response.json()
        assert response.json()["id"] == str(newest.id)
        assert response.json()["payload"]["metric_runs"] is None

    def test_metric_usage_returns_only_that_metrics_slice(self) -> None:
        mrr = upsert_metric(team=self.team, user=self.user, name="mrr", description="Monthly revenue")
        arr = upsert_metric(team=self.team, user=self.user, name="arr", description="Annual revenue")
        self._snapshot(
            payload=UsageSnapshotPayload(
                metric_runs=[
                    MetricRunCounts(
                        metric_id=mrr.id,
                        direct_runs={"web": 3, "mcp": 5},
                        direct_people=4,
                        scout_runs=120,
                    ),
                    MetricRunCounts(
                        metric_id=arr.id,
                        direct_runs={"web": 9},
                        direct_people=1,
                        scout_runs=0,
                    ),
                ],
                adjusted_runs=[
                    _adjusted(mrr.id),
                    _adjusted(mrr.id, is_scout=True),
                    _adjusted(arr.id),
                ],
            )
        )

        with patch(_FLAG, return_value=True):
            response = self.client.get(f"/api/projects/{self.team.id}/data_catalog/metrics/mrr/usage/")

        assert response.status_code == status.HTTP_200_OK, response.json()
        body = response.json()
        assert body["runs"]["direct_runs"] == {"web": 3, "mcp": 5}
        assert body["runs"]["scout_runs"] == 120
        assert [(run["metric_id"], run["is_scout"]) for run in body["adjusted_runs"]] == [
            (str(mrr.id), False),
            (str(mrr.id), True),
        ]
