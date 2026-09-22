from dataclasses import replace
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.domain.models import SyncSummary
from stocking_sheet_sync.infrastructure.queue import QueuedTask
from stocking_sheet_sync.services.sync import SyncBusyError
from stocking_sheet_sync.services.worker import process_one
from tests.services.test_sync_service import make_config


def setup(tmp_path, attempts=1):
    queue = Mock()
    queue.take.return_value = QueuedTask("1-0", "rec_test")
    queue.status.return_value = {"attempts": str(attempts - 1)}
    queue.begin.return_value = attempts
    return queue, Mock(), replace(make_config(tmp_path), queue_max_attempts=3)


@pytest.mark.parametrize("result", ["copied", "unchanged", "skipped"])
def test_completed_tasks_are_acknowledged_including_fill_degradation(tmp_path, result):
    queue, service, config = setup(tmp_path)
    service.run_record.return_value = SyncSummary(result=result, fill_degraded=True)
    assert not process_one(queue, service, config)
    queue.finish.assert_called_once()
    assert queue.finish.call_args.args[1] == "completed"
    queue.defer.assert_not_called()


def test_busy_does_not_consume_retry_budget(tmp_path):
    queue, service, config = setup(tmp_path)
    service.run_record.side_effect = SyncBusyError()
    assert process_one(queue, service, config)
    queue.defer.assert_called_once_with(
        queue.take.return_value, "已有搬运任务占用运行锁", busy=True
    )
    queue.finish.assert_not_called()


@pytest.mark.parametrize("raises", [True, False])
@pytest.mark.parametrize("attempt", [1, 3])
def test_failed_execution_retries_with_limit(tmp_path, raises, attempt):
    queue, service, config = setup(tmp_path, attempt)
    if raises:
        service.run_record.side_effect = ConnectionError("unavailable")
    else:
        service.run_record.return_value = SyncSummary(result="failed", reason="核对占位")
    assert process_one(queue, service, config) == (attempt < 3)
    if attempt < 3:
        queue.defer.assert_called_once()
        queue.finish.assert_not_called()
    else:
        assert queue.finish.call_args.args[1] == "failed"


def test_result_save_failure_keeps_unconfirmed_task(tmp_path):
    queue, service, config = setup(tmp_path)
    service.run_record.return_value = SyncSummary(result="copied")
    queue.finish.side_effect = ConnectionError("Redis unavailable")
    with pytest.raises(ConnectionError):
        process_one(queue, service, config)
    queue.defer.assert_not_called()


def test_recovered_task_at_attempt_limit_requires_manual_review(tmp_path):
    queue, service, config = setup(tmp_path, 4)
    assert not process_one(queue, service, config)
    service.run_record.assert_not_called()
    assert queue.finish.call_args.args[1] == "failed"
