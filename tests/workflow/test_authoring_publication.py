"""Authoring model work is detached from atomic version/session publication."""

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.authoring import AuthoringService
from llm_agent_eval.contracts import RevisionConflict
from llm_agent_eval.sessions import SessionStore
from llm_agent_eval.versions import VersionStore


def prepared(platform):
    seeded = platform.seed("authored_session")
    actor = Actor("local-owner", "workspace_a", "owner")
    service = AuthoringService(platform.store, platform.gateway)
    command = {
        "session_id": seeded["session_id"], "expected_revision": 0,
        "message": "make the latency limit two seconds",
    }
    return seeded, actor, service, command, service.prepare(actor, command)


def test_session_update_failure_rolls_back_new_version_and_head(platform, monkeypatch):
    seeded, actor, service, command, proposal = prepared(platform)
    before = VersionStore(platform.store).list("evaluation", seeded["evaluation_id"], actor)

    def fail_advance(*args, **kwargs):
        raise RevisionConflict(1)

    monkeypatch.setattr(SessionStore, "advance", fail_advance)
    with pytest.raises(RevisionConflict):
        service.publish(actor, command, proposal)
    after = VersionStore(platform.store).list("evaluation", seeded["evaluation_id"], actor)
    assert after == before
    assert SessionStore(platform.store).get(actor, seeded["session_id"])["revision"] == 0


def test_proposal_cannot_publish_against_newer_evaluation_head(platform):
    seeded, actor, service, command, proposal = prepared(platform)
    versions = VersionStore(platform.store)
    base = versions.get(seeded["evaluation_version_id"], actor)
    newer = versions.create("evaluation", seeded["evaluation_id"], base.content, base.revision, actor)
    with pytest.raises(RevisionConflict):
        service.publish(actor, command, proposal)
    listing = versions.list("evaluation", seeded["evaluation_id"], actor)
    assert listing["active_version_id"] == newer.version_id
    assert SessionStore(platform.store).get(actor, seeded["session_id"])["revision"] == 0
