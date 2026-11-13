"""Business failures, named after the rule they report."""

from hinoki import Conflict


class TaskAlreadyDone(Conflict):
    code = "task_already_done"
    message = "The task is already done."


class DoneTasksAreKept(Conflict):
    code = "done_tasks_are_kept"
    message = "Completed tasks are kept as a record and cannot be deleted."
