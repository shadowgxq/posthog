import itertools
from uuid import UUID

import pytest
from posthog.test.base import BaseTest

from parameterized import parameterized

from posthog.constants import AvailableFeature
from posthog.models import Organization, OrganizationMembership, Team, User
from posthog.models.file_system.file_system import FileSystem
from posthog.synthetic_user import SyntheticUser
from posthog.user_permissions import UserPermissions

from products.access_control.backend.facade.testing import add_role_member, create_access_control, create_role
from products.access_control.backend.facade.user_access_control import UserAccessControl, visible_teams_for_user
from products.authorization.backend.facade import api as authorization
from products.authorization.backend.facade.contracts import ObjectIds, Resource, Subject
from products.authorization.backend.facade.enums import Action, ResourceType
from products.authorization.backend.logic import session
from products.authorization.backend.presentation.querysets import filter_file_system_queryset, filter_queryset
from products.dashboards.backend.models.dashboard import Dashboard

OBJECT_SCENARIOS = [
    "no_rules",
    "resource_none",
    "object_none",
    "member_viewer_on_object_with_resource_none",
    "role_viewer_on_object_with_resource_none",
    "creator_with_object_none",
    "org_admin_with_object_none",
    "feature_off_with_object_none",
]

SUBJECTS = ["member", "request_user", "former_member", "service_principal"]


class TestListsMatchCurrentRules(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self._set_access_control_feature(True)
        self.other_user = User.objects.create_and_join(self.organization, "other@posthog.com", "testtest")
        self.shared = Dashboard.objects.create(team=self.team, name="Shared", created_by=self.other_user)
        self.restricted = Dashboard.objects.create(team=self.team, name="Restricted", created_by=self.other_user)
        self.own = Dashboard.objects.create(team=self.team, name="Own", created_by=self.user)

    def _set_access_control_feature(self, enabled: bool) -> None:
        features = [AvailableFeature.ACCESS_CONTROL, AvailableFeature.ROLE_BASED_ACCESS] if enabled else []
        self.organization.available_product_features = [{"key": feature, "name": feature} for feature in features]
        self.organization.save()

    def _rule(
        self,
        resource: str,
        level: str,
        resource_id: str | None = None,
        *,
        member: bool = False,
        role_id: UUID | None = None,
        team: Team | None = None,
    ) -> None:
        create_access_control(
            team_id=(team or self.team).id,
            resource=resource,
            access_level=level,
            resource_id=resource_id,
            organization_member_id=self.organization_membership.id if member else None,
            role_id=role_id,
        )

    def _make_org_admin(self) -> None:
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

    def _set_up(self, scenario: str) -> None:
        restricted_id = str(self.restricted.id)
        if scenario == "resource_none":
            self._rule("dashboard", "none")
        elif scenario in ("object_none", "creator_with_object_none", "org_admin_with_object_none"):
            self._rule("dashboard", "none", restricted_id)
        elif scenario == "member_viewer_on_object_with_resource_none":
            self._rule("dashboard", "none")
            self._rule("dashboard", "viewer", restricted_id, member=True)
        elif scenario == "role_viewer_on_object_with_resource_none":
            role_id = create_role(organization_id=self.organization.id, name="Finance")
            add_role_member(role_id=role_id, user_id=self.user.id)
            self._rule("dashboard", "none")
            self._rule("dashboard", "viewer", restricted_id, role_id=role_id)
        elif scenario == "feature_off_with_object_none":
            self._rule("dashboard", "none", restricted_id)
            self._set_access_control_feature(False)
        if scenario == "creator_with_object_none":
            self.restricted.created_by = self.user
            self.restricted.save()
        if scenario == "org_admin_with_object_none":
            self._make_org_admin()

    @parameterized.expand(
        [
            (
                f"{scenario}_{action.value}_{'most_specific' if most_specific else 'legacy'}_{subject}",
                scenario,
                action,
                most_specific,
                subject,
            )
            for scenario, action, most_specific, subject in itertools.product(
                OBJECT_SCENARIOS, [Action.VIEW, Action.QUERY], [True, False], SUBJECTS
            )
        ]
    )
    def test_list_objects_matches_v1_list_filter(
        self, _name: str, scenario: str, action: Action, most_specific: bool, subject: str
    ) -> None:
        Organization.objects.filter(pk=self.organization.pk).update(uses_most_specific_access_resolution=most_specific)
        self.organization.refresh_from_db()
        self._set_up(scenario)
        former_member = User.objects.create_and_join(
            Organization.objects.create(name="Other"), "former@posthog.com", "x"
        )
        Dashboard.objects.create(team=self.team, name="Former member's", created_by=former_member)
        dashboards = Dashboard.objects.filter(team=self.team)

        result = self._list_dashboards(subject, action, former_member)

        if subject in ("former_member", "service_principal"):
            # Only a project member lists objects, as a view's permission class requires.
            expected = set()
        else:
            # QUERY follows the query guard, which gives an org admin every object, as the list filter's admin flag does.
            expected = set(
                UserAccessControl(self.user, self.team)
                .filter_queryset_by_access_level(dashboards, include_all_if_admin=action == Action.QUERY)
                .values_list("id", flat=True)
            )
        listed = {
            dashboard.id
            for dashboard in dashboards
            if result.allows(str(dashboard.id), created_by_user_id=dashboard.created_by_id)
        }
        assert listed == expected

    def _list_dashboards(self, subject: str, action: Action, former_member: User) -> ObjectIds:
        resource = Resource(type=ResourceType.DASHBOARD, project_id=self.team.id)
        if subject in ("member", "former_member"):
            user = self.user if subject == "member" else former_member
            return authorization.list_objects(
                resource=resource, action=action, subject=Subject(user_id=user.id), creator_exempt=True
            )
        principal = self.user if subject == "request_user" else SyntheticUser(self.team, "service")
        with session.scope(lambda: principal):
            return authorization.list_objects(resource=resource, action=action, creator_exempt=True)

    @parameterized.expand(
        [
            ("member", False, False, False),
            ("member_without_resource_access", False, False, True),
            ("org_admin", True, False, False),
            ("org_admin_include_all", True, True, False),
        ]
    )
    def test_filter_queryset_runs_the_v1_filter(
        self, _name: str, admin: bool, include_all_if_admin: bool, resource_none: bool
    ) -> None:
        if resource_none:
            self._rule("dashboard", "none")
            self._rule("dashboard", "viewer", str(self.shared.id), member=True)
        else:
            self._rule("dashboard", "none", str(self.restricted.id))
        if admin:
            self._make_org_admin()
        dashboards = Dashboard.objects.filter(team=self.team)

        filtered = filter_queryset(
            dashboards,
            resource=Resource(type=ResourceType.DASHBOARD, project_id=self.team.id),
            subject=Subject(user_id=self.user.id),
            include_all_if_admin=include_all_if_admin,
        )

        expected = UserAccessControl(self.user, self.team).filter_queryset_by_access_level(
            dashboards, include_all_if_admin=include_all_if_admin
        )
        assert str(filtered.query) == str(expected.query)

    def test_filter_file_system_queryset_runs_the_v1_filter(self) -> None:
        self._rule("dashboard", "none", str(self.restricted.id))
        entries = FileSystem.objects.filter(team=self.team)
        denied_refs = {("dashboard", self.team.id): [str(self.own.id)]}

        filtered = filter_file_system_queryset(
            entries, project_id=self.team.id, subject=Subject(user_id=self.user.id), extra_denied_refs=denied_refs
        )

        expected = UserAccessControl(self.user, self.team).filter_and_annotate_file_system_queryset(
            entries, denied_refs
        )
        assert str(filtered.query) == str(expected.query)

    def test_filter_file_system_queryset_shows_a_non_member_nothing(self) -> None:
        outsider = User.objects.create_and_join(Organization.objects.create(name="Other"), "outsider@posthog.com", "x")
        FileSystem.objects.create(
            team=self.team, path="Shared", type="dashboard", ref=str(self.shared.id), created_by=self.other_user
        )
        entries = FileSystem.objects.filter(team=self.team)

        filtered = filter_file_system_queryset(entries, project_id=self.team.id, subject=Subject(user_id=outsider.id))

        assert entries.exists()
        assert not filtered.exists()

    @parameterized.expand([("member", False, False), ("private_project", True, False), ("org_admin", True, True)])
    def test_project_list_matches_visible_teams(self, _name: str, private: bool, admin: bool) -> None:
        private_team = Team.objects.create(organization=self.organization, name="Private")
        Team.objects.create(organization=Organization.objects.create(name="Elsewhere"), name="Foreign")
        if private:
            self._rule("project", "none", str(private_team.id), team=private_team)
        if admin:
            self._make_org_admin()

        result = authorization.list_objects(
            resource=Resource(type=ResourceType.PROJECT, organization_id=self.organization.id),
            action=Action.VIEW,
            subject=Subject(user_id=self.user.id),
        )

        expected = visible_teams_for_user(
            self.organization,
            UserAccessControl(user=self.user, organization_id=str(self.organization.id)),
            UserPermissions(self.user),
        )
        assert not result.excluded
        assert result.ids == {str(team_id) for team_id in expected.values_list("id", flat=True)}

    def test_list_over_the_limit_allows_nothing(self) -> None:
        self._rule("dashboard", "none")
        self._rule("dashboard", "viewer", str(self.shared.id), member=True)
        self._rule("dashboard", "viewer", str(self.restricted.id), member=True)

        result = authorization.list_objects(
            resource=Resource(type=ResourceType.DASHBOARD, project_id=self.team.id),
            action=Action.VIEW,
            subject=Subject(user_id=self.user.id),
            limit=1,
        )

        assert not result.complete
        assert not result.allows(str(self.shared.id))

    @parameterized.expand(
        [
            ("edit", Resource(type=ResourceType.DASHBOARD, project_id=1), Action.EDIT, NotImplementedError),
            ("one_object", Resource(type=ResourceType.DASHBOARD, id="1", project_id=1), Action.VIEW, ValueError),
            ("no_project", Resource(type=ResourceType.DASHBOARD), Action.VIEW, ValueError),
            ("organizations", Resource(type=ResourceType.ORGANIZATION), Action.VIEW, ValueError),
            ("project_without_organization", Resource(type=ResourceType.PROJECT), Action.VIEW, ValueError),
            ("project_query", Resource(type=ResourceType.PROJECT), Action.QUERY, NotImplementedError),
        ]
    )
    def test_list_rejects_what_the_current_rules_cannot_list(
        self, _name: str, resource: Resource, action: Action, error: type[Exception]
    ) -> None:
        with pytest.raises(error):
            authorization.list_objects(resource=resource, action=action, subject=Subject(user_id=self.user.id))
