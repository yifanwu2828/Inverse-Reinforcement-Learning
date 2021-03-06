import abc


class BaseAgent(object, metaclass=abc.ABCMeta):
    def __init__(self, **kwargs):
        super(BaseAgent, self).__init__(**kwargs)

    def __repr__(self) -> str:
        return f"BaseAgent"

    def train(self) -> dict:
        """Return a dictionary of logging information."""
        raise NotImplementedError

    def add_to_replay_buffer(self, paths):
        raise NotImplementedError

    def sample(self, batch_size):
        raise NotImplementedError

    def save(self, path):
        raise NotImplementedError
