import pytest

from db.accounts import create_account, get_account, get_all_credentials, list_accounts, save_account, set_account_role
from db.projects import create_project
from main import async_main, build_parser


@pytest.mark.parametrize("network,role", [("twitter", "source_and_destination"), ("rss", "source_only"), ("instagram", "source_only")])
def test_new_connection_role_defaults(engine, network, role):
    assert create_account(engine, network, "main", "remote").sync_role == role
    assert save_account(engine, network, "auth", "other", {}).sync_role == role


@pytest.mark.parametrize("network", ["rss", "instagram"])
@pytest.mark.parametrize("role", ["destination_only", "source_and_destination"])
def test_read_only_networks_cannot_receive_posts(engine, network, role):
    account = create_account(engine, network, "main", "remote")
    with pytest.raises(ValueError, match="must be source_only"):
        set_account_role(engine, account.id, role)
    with pytest.raises(ValueError, match="must be source_only"):
        save_account(engine, network, "main", "changed", {"token": "changed"}, sync_role=role)
    assert get_account(engine, account.id) == account
    assert get_all_credentials(engine, account.id) == {}


@pytest.mark.parametrize("first_role", ["source_only", "source_and_destination", "destination_only"])
@pytest.mark.parametrize("second_role", ["source_only", "source_and_destination", "destination_only"])
def test_shared_remote_account_requires_every_connection_to_be_destination_only(engine, first_role, second_role):
    project = create_project(engine, "Work")
    first = save_account(engine, "twitter", "main", "alice", {"token": "first"}, sync_role=first_role)
    if first_role == second_role == "destination_only":
        second = save_account(engine, "twitter", "main", "alice", {"token": "second"}, project_id=project.id, sync_role=second_role)
        assert first.id != second.id
        assert get_all_credentials(engine, first.id) == {"token": "first"}
        assert get_all_credentials(engine, second.id) == {"token": "second"}
    else:
        with pytest.raises(ValueError, match="shared across projects"):
            save_account(engine, "twitter", "main", "alice", {"token": "second"}, project_id=project.id, sync_role=second_role)
        assert list_accounts(engine) == [first]


@pytest.mark.parametrize("role", ["source_only", "source_and_destination"])
def test_shared_destination_cannot_become_a_source(engine, role):
    project = create_project(engine, "Work")
    first = create_account(engine, "twitter", "main", "alice", sync_role="destination_only")
    create_account(engine, "twitter", "main", "alice", project_id=project.id, sync_role="destination_only")
    with pytest.raises(ValueError, match="shared across projects"):
        set_account_role(engine, first.id, role)
    assert get_account(engine, first.id).sync_role == "destination_only"


@pytest.mark.parametrize("role", ["source_only", "destination_only", "source_and_destination"])
def test_reauthentication_preserves_role_when_not_specified(engine, role):
    account = save_account(engine, "twitter", "main", "alice", {"token": "old"}, sync_role=role)
    updated = save_account(engine, "twitter", "main", "alice", {"token": "new"})
    assert updated.id == account.id
    assert updated.sync_role == role
    assert get_all_credentials(engine, account.id)["token"] == "new"


async def test_cli_changes_only_selected_connection_role_without_auth(engine, monkeypatch):
    project = create_project(engine, "Work")
    first = create_account(engine, "twitter", "main", "alice")
    second = create_account(engine, "twitter", "main", "bob", project_id=project.id)
    monkeypatch.setattr("main.get_engine", lambda: engine)
    await async_main(build_parser().parse_args([
        "--project", "Work", "--configure-account", "twitter", "--label", "main", "--sync-role", "source_only",
    ]))
    assert get_account(engine, first.id).sync_role == "source_and_destination"
    assert get_account(engine, second.id).sync_role == "source_only"


@pytest.mark.parametrize("role", ["", "both", "invalid"])
def test_unknown_roles_are_rejected(engine, role):
    with pytest.raises(ValueError, match="Unknown sync role"):
        create_account(engine, "twitter", "main", "alice", sync_role=role)


def test_same_mastodon_id_on_different_instances_can_be_sources_in_separate_projects(engine):
    project = create_project(engine, "Work")
    save_account(engine, "mastodon", "main", "123", {"instance_url": "https://one.test", "account_id": "123"})
    other = save_account(engine, "mastodon", "main", "123", {"instance_url": "https://two.test", "account_id": "123"}, project_id=project.id)
    assert other.sync_role == "source_and_destination"
    with pytest.raises(ValueError, match="shared across projects"):
        save_account(engine, "mastodon", "main", "123", {"instance_url": "https://one.test", "account_id": "123"}, project_id=project.id)
    assert get_all_credentials(engine, other.id)["instance_url"] == "https://two.test"
