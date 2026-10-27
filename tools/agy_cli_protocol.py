"""Shared CLI input-turn accounting, independent of the web app and debugger."""


class CliTurnCounter:
    """Internal finish reminders also increment the CLI's cumulative num_turns."""

    def __init__(self):
        self.completed = 0
        self._last_input_step = -1
        self._inputs = set()

    def begin(self):
        self._inputs.clear()

    def observe(self, step):
        if step.get("step_type") != "user_input":
            return
        index = step.get("step_index")
        if type(index) is not int or index < 0:
            raise ValueError("CLI user input has an invalid step index")
        if index <= self._last_input_step:
            raise ValueError("CLI repeated a user input from a completed turn")
        # ACTIVE/DONE updates for the same input count only once.
        self._inputs.add(index)

    @property
    def input_count(self):
        # Older/minimal event streams may omit user_input updates. They can
        # confirm one submitted input, but cannot justify extra CLI turns.
        return max(1, len(self._inputs))

    @property
    def expected(self):
        return self.completed + self.input_count

    def confirm(self, value):
        expected = self.expected
        if type(value) is not int or value != expected:
            raise ValueError(f"CLI turn number mismatch: expected {expected}, received {value!r}")
        self.completed = expected
        self._last_input_step = max(self._inputs, default=self._last_input_step)
