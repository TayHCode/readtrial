import os
import tempfile

tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
