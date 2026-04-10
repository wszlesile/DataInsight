from config.config import Config


def __getattr__(name: str):
    if name == 'create_app':
        from config.factory import create_app
        return create_app
    raise AttributeError(name)

__all__ = ['Config', 'create_app']
