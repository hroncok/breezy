# Copyright (C) 2005 Robey Pointer <robey@lag.net>, Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

import os
import socket
import threading

import bzrlib.bzrdir as bzrdir
import bzrlib.errors as errors
from bzrlib.osutils import pathjoin, lexists
from bzrlib.tests import TestCaseWithTransport, TestCase, TestSkipped
import bzrlib.transport
from bzrlib.workingtree import WorkingTree

try:
    import paramiko
    paramiko_loaded = True
except ImportError:
    paramiko_loaded = False


class TestCaseWithSFTPServer(TestCaseWithTransport):
    """A test case base class that provides a sftp server on localhost."""

    def setUp(self):
        if not paramiko_loaded:
            raise TestSkipped('you must have paramiko to run this test')
        super(TestCaseWithSFTPServer, self).setUp()
        from bzrlib.transport.sftp import SFTPAbsoluteServer, SFTPHomeDirServer
        if getattr(self, '_get_remote_is_absolute', None) is None:
            self._get_remote_is_absolute = True
        if self._get_remote_is_absolute:
            self.transport_server = SFTPAbsoluteServer
        else:
            self.transport_server = SFTPHomeDirServer
        self.transport_readonly_server = bzrlib.transport.http.HttpServer

    def get_transport(self, path=None):
        """Return a transport relative to self._test_root."""
        return bzrlib.transport.get_transport(self.get_url(path))


class SFTPLockTests (TestCaseWithSFTPServer):

    def test_sftp_locks(self):
        from bzrlib.errors import LockError
        t = self.get_transport()

        l = t.lock_write('bogus')
        self.failUnlessExists('bogus.write-lock')

        # Don't wait for the lock, locking an already locked
        # file should raise an assert
        self.assertRaises(LockError, t.lock_write, 'bogus')

        l.unlock()
        self.failIf(lexists('bogus.write-lock'))

        open('something.write-lock', 'wb').write('fake lock\n')
        self.assertRaises(LockError, t.lock_write, 'something')
        os.remove('something.write-lock')

        l = t.lock_write('something')

        l2 = t.lock_write('bogus')

        l.unlock()
        l2.unlock()

    def test_multiple_connections(self):
        t = self.get_transport()
        self.assertTrue('sftpserver - new connection' in self.get_server().logs)
        self.get_server().logs = []
        # The second request should reuse the first connection
        # SingleListener only allows for a single connection,
        # So the next line fails unless the connection is reused
        t2 = self.get_transport()
        self.assertEquals(self.get_server().logs, [])


class SFTPTransportTestRelative(TestCaseWithSFTPServer):
    """Test the SFTP transport with homedir based relative paths."""

    def test__remote_path(self):
        t = self.get_transport()
        # try what is currently used:
        # remote path = self._abspath(relpath)
        self.assertEqual(self.test_dir + '/relative', t._remote_path('relative'))
        # we dont os.path.join because windows gives us the wrong path
        root_segments = self.test_dir.split('/')
        root_parent = '/'.join(root_segments[:-1])
        # .. should be honoured
        self.assertEqual(root_parent + '/sibling', t._remote_path('../sibling'))
        # /  should be illegal ?
        ### FIXME decide and then test for all transports. RBC20051208


class SFTPTransportTestRelative(TestCaseWithSFTPServer):
    """Test the SFTP transport with homedir based relative paths."""

    def setUp(self):
        self._get_remote_is_absolute = False
        super(SFTPTransportTestRelative, self).setUp()

    def test__remote_path_relative_root(self):
        # relative paths are preserved
        t = self.get_transport('')
        self.assertEqual('a', t._remote_path('a'))


class FakeSFTPTransport (object):
    _sftp = object()
fake = FakeSFTPTransport()


class SFTPNonServerTest(TestCase):
    def setUp(self):
        TestCase.setUp(self)
        if not paramiko_loaded:
            raise TestSkipped('you must have paramiko to run this test')

    def test_parse_url(self):
        from bzrlib.transport.sftp import SFTPTransport
        s = SFTPTransport('sftp://simple.example.com/home/source', clone_from=fake)
        self.assertEquals(s._host, 'simple.example.com')
        self.assertEquals(s._port, None)
        self.assertEquals(s._path, '/home/source')
        self.failUnless(s._password is None)

        self.assertEquals(s.base, 'sftp://simple.example.com/home/source/')

        s = SFTPTransport('sftp://ro%62ey:h%40t@example.com:2222/~/relative', clone_from=fake)
        self.assertEquals(s._host, 'example.com')
        self.assertEquals(s._port, 2222)
        self.assertEquals(s._username, 'robey')
        self.assertEquals(s._password, 'h@t')
        self.assertEquals(s._path, 'relative')

        # Base should not keep track of the password
        self.assertEquals(s.base, 'sftp://robey@example.com:2222/~/relative/')

    def test_relpath(self):
        from bzrlib.transport.sftp import SFTPTransport
        from bzrlib.errors import PathNotChild

        s = SFTPTransport('sftp://user@host.com/abs/path', clone_from=fake)
        self.assertEquals(s.relpath('sftp://user@host.com/abs/path/sub'), 'sub')
        # Can't test this one, because we actually get an AssertionError
        # TODO: Consider raising an exception rather than an assert
        #self.assertRaises(PathNotChild, s.relpath, 'http://user@host.com/abs/path/sub')
        self.assertRaises(PathNotChild, s.relpath, 'sftp://user2@host.com/abs/path/sub')
        self.assertRaises(PathNotChild, s.relpath, 'sftp://user@otherhost.com/abs/path/sub')
        self.assertRaises(PathNotChild, s.relpath, 'sftp://user@host.com:33/abs/path/sub')
        self.assertRaises(PathNotChild, s.relpath, 'sftp://user@host.com/~/rel/path/sub')

        # Make sure it works when we don't supply a username
        s = SFTPTransport('sftp://host.com/abs/path', clone_from=fake)
        self.assertEquals(s.relpath('sftp://host.com/abs/path/sub'), 'sub')

        # Make sure it works when parts of the path will be url encoded
        # TODO: These may be incorrect, we might need to urllib.urlencode() before
        # we pass the paths into the SFTPTransport constructor
        s = SFTPTransport('sftp://host.com/dev/,path', clone_from=fake)
        self.assertEquals(s.relpath('sftp://host.com/dev/,path/sub'), 'sub')
        s = SFTPTransport('sftp://host.com/dev/%path', clone_from=fake)
        self.assertEquals(s.relpath('sftp://host.com/dev/%path/sub'), 'sub')

    def test_parse_invalid_url(self):
        from bzrlib.transport.sftp import SFTPTransport, TransportError
        try:
            s = SFTPTransport('sftp://lilypond.org:~janneke/public_html/bzr/gub',
                              clone_from=fake)
            self.fail('expected exception not raised')
        except TransportError, e:
            self.assertEquals(str(e), 
                    'Transport error: ~janneke: invalid port number ')


class SFTPBranchTest(TestCaseWithSFTPServer):
    """Test some stuff when accessing a bzr Branch over sftp"""

    def test_lock_file(self):
        # old format branches use a special lock file on sftp.
        b = self.make_branch('', format=bzrdir.BzrDirFormat6())
        b = bzrlib.branch.Branch.open(self.get_url())
        self.failUnlessExists('.bzr/')
        self.failUnlessExists('.bzr/branch-format')
        self.failUnlessExists('.bzr/branch-lock')

        self.failIf(lexists('.bzr/branch-lock.write-lock'))
        b.lock_write()
        self.failUnlessExists('.bzr/branch-lock.write-lock')
        b.unlock()
        self.failIf(lexists('.bzr/branch-lock.write-lock'))

    def test_push_support(self):
        self.build_tree(['a/', 'a/foo'])
        t = bzrdir.BzrDir.create_standalone_workingtree('a')
        b = t.branch
        t.add('foo')
        t.commit('foo', rev_id='a1')

        b2 = bzrdir.BzrDir.create_branch_and_repo(self.get_url('/b'))
        b2.pull(b)

        self.assertEquals(b2.revision_history(), ['a1'])

        open('a/foo', 'wt').write('something new in foo\n')
        t.commit('new', rev_id='a2')
        b2.pull(b)

        self.assertEquals(b2.revision_history(), ['a1', 'a2'])


class SFTPFullHandshakingTest(TestCaseWithSFTPServer):
    """Verify that a full-handshake (SSH over loopback TCP) sftp connection works."""
    
    def test_connection(self):
        from bzrlib.transport.sftp import SFTPFullAbsoluteServer
        self.transport_server = SFTPFullAbsoluteServer
        self.get_transport()
