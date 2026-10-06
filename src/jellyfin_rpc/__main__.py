import sys

if getattr(sys, 'frozen', False):
    from .app import main as entrypoint
else:
    from .main import main as entrypoint

if __name__ == '__main__':
    entrypoint()
