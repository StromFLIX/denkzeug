"""Cleanup must preserve deployment proof and never retire unfinished work."""

import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautionette.temporal_state import (
    COMPLETED_RECEIPT,
    MAX_ARCHIVE_BYTES,
    MAX_RECEIPT_BYTES,
    completed_receipt,
    controller_state,
)

from scripts import temporal_deploy_job as job

COMMIT = "b" * 40


class MissingReceipt(Exception):
    response = SimpleNamespace(status_code=404)


def receipt_archive(value, *, name="completed.json", kind=tarfile.REGTYPE):
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    return result.getvalue()


def controller(value=None, *, status="exited", archive=None):
    container = SimpleNamespace(
        labels={"nautionette.temporal.commit": COMMIT},
        status=status,
        reload=Mock(),
        exec_run=Mock(),
        update=Mock(),
        stop=Mock(),
    )
    if value is None and archive is None:
        container.get_archive = Mock(side_effect=MissingReceipt())
    else:
        data = archive if archive is not None else receipt_archive(value)
        container.get_archive = Mock(side_effect=lambda path: (iter([data]), {}))
    return container


def test_stopped_controller_health_reads_only_small_public_receipt():
    value = {"status": "completed", "commit": COMMIT, "target": "1.32.2", "shards": "4", "secret": "private"}
    container = controller(value)
    result = controller_state(container)
    assert result == {key: value[key] for key in ("status", "commit", "target", "shards")}
    container.get_archive.assert_called_once_with(COMPLETED_RECEIPT)
    container.exec_run.assert_not_called()


def test_legacy_running_controller_is_readable_but_missing_stopped_receipt_fails_closed():
    container = controller(status="running")
    container.exec_run.return_value = SimpleNamespace(
        exit_code=0,
        output=json.dumps(
            {
                "status": "prepared",
                "commit": COMMIT,
                "secret": "private",
            }
        ).encode(),
    )
    assert controller_state(container) == {"status": "prepared", "commit": COMMIT}
    assert container.exec_run.call_args.args[0][-1] == "status"
    container.status = "exited"
    with pytest.raises(RuntimeError, match="no verified receipt"):
        controller_state(container)
    assert container.exec_run.call_count == 1


@pytest.mark.parametrize(
    "value",
    [
        {"status": "prepared", "commit": COMMIT},
        {"status": "completed", "commit": "c" * 40},
        {"status": "completed"},
        [],
        "not a receipt",
    ],
)
def test_unfinished_wrong_revision_or_malformed_receipt_never_proves_completion(value):
    container = controller(value)
    with pytest.raises((ValueError, TypeError)):
        completed_receipt(container)
    container.exec_run.assert_not_called()


@pytest.mark.parametrize(
    "archive",
    [
        b"x" * (MAX_ARCHIVE_BYTES + 1),
        receipt_archive(b"x" * (MAX_RECEIPT_BYTES + 1)),
        receipt_archive({}, name="../state.json"),
        receipt_archive({}, kind=tarfile.SYMTYPE),
        receipt_archive(b"not json"),
    ],
)
def test_archive_reader_rejects_oversize_paths_links_and_invalid_json(archive):
    with pytest.raises((ValueError, tarfile.TarError)):
        completed_receipt(controller(archive=archive))


# Retirement and duplicate finish are native launcher tests in
# src/parity_tests/maintenance_cases.rs. The controller's shared reader stays here.


def test_post_hook_waits_for_service_startup_without_repeating_migrations(monkeypatch):
    monkeypatch.setattr(job, "location", lambda: "run")
    finish = Mock(side_effect=[RuntimeError("services starting"), None])
    receipt = Mock()
    monkeypatch.setattr(job.host, "finish", finish)
    monkeypatch.setattr(job, "write_completed_receipt", receipt)
    monkeypatch.setattr(job.time, "sleep", Mock())
    monkeypatch.setattr(job.time, "monotonic", Mock(side_effect=[0, 1]))
    job.verify_and_complete()
    assert finish.call_count == 2
    receipt.assert_called_once()
    monkeypatch.setattr(job.host, "finish", Mock(side_effect=RuntimeError("wrong deployed revision")))
    monkeypatch.setattr(job.time, "monotonic", Mock(side_effect=[0, 181]))
    receipt.reset_mock()
    with pytest.raises(RuntimeError, match="wrong deployed revision"):
        job.verify_and_complete()
    receipt.assert_not_called()


def test_job_publishes_receipt_only_after_verified_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(job, "COMPLETED_RECEIPT", str(tmp_path / "completed.json"))
    monkeypatch.setenv("MAINTENANCE_COMMIT", COMMIT)
    monkeypatch.setattr(job, "status", lambda: {"status": "prepared", "commit": COMMIT})
    with pytest.raises(RuntimeError, match="incomplete"):
        job.write_completed_receipt()
    assert not (tmp_path / "completed.json").exists()
    monkeypatch.setattr(job, "status", lambda: {"status": "completed", "commit": COMMIT})
    job.write_completed_receipt()
    assert json.loads((tmp_path / "completed.json").read_text())["status"] == "completed"
    assert (tmp_path / "completed.json").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "completed.tmp").exists()
