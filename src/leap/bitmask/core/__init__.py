import platform
import os

# FIXME some temporary imports to make the modules
# appear in the coverage report. Remove the imports when
# test code cover them.


def dummy_imports():
    import service
    import dispatcher
    try:
        import uuid_map
        import mail_services
    except ImportError:
        pass


APPNAME = "bitmask.core"
if platform.system() == 'Windows':
    ENDPOINT = "tcp://127.0.0.1:5001"
else:
    ENDPOINT = "ipc:///var/tmp/%s.sock" % APPNAME

dummy_imports()
