from llm_agent_eval.targets.http_job import AsyncJobAdapter
from llm_agent_eval.targets.http_job import HttpJobAdapter
from llm_agent_eval.targets.network_policy import EndpointPolicy


def test_async_adapter_persists_remote_id_and_is_restart_safe():
    adapter = AsyncJobAdapter(submit=lambda payload: {"id": "remote-1"}, poll=lambda job_id: {"state": "completed", "output": {"ok": True}})
    submitted = adapter.submit({"q": "hi"})
    assert submitted.remote_job_id == "remote-1"
    assert adapter.poll(submitted.remote_job_id).outcome == "ok"
    assert adapter.cancel(submitted.remote_job_id).state == "uncertain"


def test_http_job_resume_polls_existing_job_without_duplicate_submit(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"state": "completed", "output": {"ok": True}}

    class Client:
        submits = 0

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *_args, **_kwargs):
            type(self).submits += 1
            return type("SubmitResponse", (), {"status_code": 202, "json": lambda _self: {"id": "job-1"}})()

        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("llm_agent_eval.targets.http_job.httpx.Client", Client)
    policy = EndpointPolicy(
        allow_exact={"provider.test:80"},
        resolver=lambda host, port, **_kwargs: [(None, None, None, None, ("192.0.2.10", port))],
    )
    adapter = HttpJobAdapter(policy=policy)
    target = {
        "mode": "async",
        "submit_url": "http://provider.test/invoke",
        "status_url_template": "http://provider.test/jobs/{job_id}",
        "poll_timeout_seconds": 1,
        "poll_interval_seconds": 0,
    }
    persisted = []
    first = adapter.invoke(target, {"q": "one"}, {"persist_remote_job": persisted.append})
    assert first.outcome == "ok"
    assert persisted == ["job-1"]

    second = adapter.invoke(target, {"q": "one"}, {"remote_job_id": "job-1"})
    assert second.outcome == "ok"
    assert Client.submits == 1


def test_http_job_cancellation_keeps_remote_identity_uncertain(monkeypatch):
    class Response:
        status_code = 202

        def json(self):
            return {"id": "job-cancel"}

    class Client:
        submits = 0
        polls = 0

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, *_args, **_kwargs):
            type(self).submits += 1
            return Response()

        def get(self, *_args, **_kwargs):
            type(self).polls += 1
            raise AssertionError("cancelled job must not be polled")

    monkeypatch.setattr("llm_agent_eval.targets.http_job.httpx.Client", Client)
    policy = EndpointPolicy(
        allow_exact={"provider.test:80"},
        resolver=lambda host, port, **_kwargs: [(None, None, None, None, ("192.0.2.10", port))],
    )
    adapter = HttpJobAdapter(policy=policy)
    result = adapter.invoke(
        {
            "submit_url": "http://provider.test/invoke",
            "status_url_template": "http://provider.test/jobs/{job_id}",
            "poll_timeout_seconds": 1,
            "poll_interval_seconds": 0,
        },
        {"q": "one"},
        {"should_cancel": lambda: True},
    )
    assert result.outcome == "cancelled"
    assert result.remote_uncertainty == "cancelled"
    assert result.connector_observations["remote_job_id"] == "job-cancel"
