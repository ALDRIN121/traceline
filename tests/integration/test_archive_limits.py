"""Archive validation is performed before any unsafe extraction occurs."""

import io
import zipfile

import pytest

from llm_agent_eval.ingestion import ArchiveLimits, ImportFailure, _sanitized_archive, inspect_zip


def _archive(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, content in entries.items():
            zipped.writestr(name, content)
    return data.getvalue()


def test_traversal_does_not_create_a_sanitized_snapshot():
    with pytest.raises(ImportFailure) as failed:
        inspect_zip(_archive({"../../outside.py": "x = 1"}), ArchiveLimits())
    assert failed.value.code == "unsafe_archive_path"


def test_expansion_limit_rejects_member_before_materializing_content():
    with pytest.raises(ImportFailure) as failed:
        inspect_zip(_archive({"expands.txt": "a" * 4096}), ArchiveLimits(max_expanded_bytes=1024))
    assert failed.value.code == "archive_expansion_limit"


@pytest.mark.parametrize(("name", "content"), [
    ("config.json", b'{"provider_token":"sk-test-do-not-persist-0001"}'),
    ("id_rsa", b"-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"),
    ("binary.bin", b"\xff\x00sk-test-do-not-persist-0001"),
])
def test_sanitization_excludes_quoted_json_key_material_and_invalid_utf8_secrets(name, content):
    snapshot, exclusions = _sanitized_archive(inspect_zip(_archive({name: content})))
    assert b"sk-test-do-not-persist-0001" not in snapshot
    assert exclusions[0]["path"] == name
