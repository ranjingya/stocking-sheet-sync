from unittest.mock import MagicMock

import pytest
from redis.exceptions import ResponseError

from stocking_sheet_sync.infrastructure.queue import QueuedTask, TaskQueue
from tests.services.test_sync_service import make_config


def test_pending_task_is_recovered_before_reading_new_tasks(tmp_path):
    redis = MagicMock()
    queue = TaskQueue(make_config(tmp_path), client=redis)
    redis.xreadgroup.return_value = [(queue.stream, [("1-0", {"record_id": "rec_a"})])]
    assert queue.take() == QueuedTask("1-0", "rec_a")
    redis.xreadgroup.assert_called_once_with(
        queue.group, queue.consumer, {queue.stream: "0"}, count=1
    )


def test_empty_pending_reads_new_tasks_with_bounded_wait(tmp_path):
    redis = MagicMock()
    queue = TaskQueue(make_config(tmp_path), client=redis)
    redis.xreadgroup.side_effect = [[(queue.stream, [])], []]
    assert queue.take() is None
    assert redis.xreadgroup.call_args.kwargs["block"] == 1000
    assert redis.xreadgroup.call_args.args[2] == {queue.stream: ">"}


def test_completion_saves_result_and_acknowledges_in_one_transaction(tmp_path):
    redis = MagicMock()
    queue = TaskQueue(make_config(tmp_path), client=redis)
    queue.finish(QueuedTask("1-0", "rec_a"), "completed", {"result": "copied"})
    redis.pipeline.assert_called_once_with(transaction=True)
    pipe = redis.pipeline.return_value.__enter__.return_value
    pipe.hset.assert_called_once()
    pipe.expire.assert_called_once_with(queue.status_prefix + "1-0", queue.retention)
    pipe.xack.assert_called_once_with(queue.stream, queue.group, "1-0")
    pipe.xdel.assert_called_once_with(queue.stream, "1-0")
    pipe.execute.assert_called_once()


@pytest.mark.parametrize("message", ["BUSYGROUP Consumer Group name already exists", "WRONGTYPE"])
def test_group_creation_only_ignores_existing_group(tmp_path, message):
    redis = MagicMock()
    redis.xgroup_create.side_effect = ResponseError(message)
    if message.startswith("BUSYGROUP"):
        TaskQueue(make_config(tmp_path), client=redis)
        redis.close.assert_not_called()
    else:
        with pytest.raises(ResponseError):
            TaskQueue(make_config(tmp_path), client=redis)
        redis.close.assert_called_once()
