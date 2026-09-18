from llm_agent_eval.targets.http_job import AsyncJobAdapter


def test_async_adapter_persists_remote_id_and_is_restart_safe():
    adapter = AsyncJobAdapter(submit=lambda payload: {"id": "remote-1"}, poll=lambda job_id: {"state": "completed", "output": {"ok": True}})
    submitted = adapter.submit({"q": "hi"})
    assert submitted.remote_job_id == "remote-1"
    assert adapter.poll(submitted.remote_job_id).outcome == "ok"
    assert adapter.cancel(submitted.remote_job_id).state == "uncertain"
