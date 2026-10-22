import os
import unittest

posix_only = unittest.skipUnless(os.name == 'posix', 'needs POSIX process, user and mode semantics')
needs_xattr = unittest.skipUnless(hasattr(os, 'listxattr'), 'os.listxattr is unavailable on this platform')
