# Copyright (C) 2005 Robey Pointer <robey@lag.net>, Canonical Ltd

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""
A stub SFTP server for loopback SFTP testing.
Adapted from the one in paramiko's unit tests.
"""

import os
from paramiko import ServerInterface, SFTPServerInterface, SFTPServer, SFTPAttributes, \
    SFTPHandle, SFTP_OK, AUTH_SUCCESSFUL, OPEN_SUCCEEDED

from bzrlib.osutils import pathjoin
from bzrlib.trace import mutter


class StubServer (ServerInterface):

    def __init__(self, test_case):
        ServerInterface.__init__(self)
        self._test_case = test_case

    def check_auth_password(self, username, password):
        # all are allowed
        self._test_case.log('sftpserver - authorizing: %s' % (username,))
        return AUTH_SUCCESSFUL

    def check_channel_request(self, kind, chanid):
        self._test_case.log('sftpserver - channel request: %s, %s' % (kind, chanid))
        return OPEN_SUCCEEDED


class StubSFTPHandle (SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        # python doesn't have equivalents to fchown or fchmod, so we have to
        # use the stored filename
        mutter('Changing permissions on %s to %s', self.filename, attr)
        try:
            SFTPServer.set_file_attr(self.filename, attr)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)


class StubSFTPServer (SFTPServerInterface):

    def __init__(self, server, root, home=None):
        SFTPServerInterface.__init__(self, server)
        self.root = root
        if home is None:
            self.home = self.root
        else:
            self.home = home[len(self.root):]
        if (len(self.home) > 0) and (self.home[0] == '/'):
            self.home = self.home[1:]
        server._test_case.log('sftpserver - new connection')

    def _realpath(self, path):
        return self.root + self.canonicalize(path)

    def canonicalize(self, path):
        if os.path.isabs(path):
            return os.path.normpath(path)
        else:
            return os.path.normpath('/' + os.path.join(self.home, path))

    def chattr(self, path, attr):
        try:
            SFTPServer.set_file_attr(path, attr)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def list_folder(self, path):
        path = self._realpath(path)
        try:
            out = [ ]
            # TODO: win32 incorrectly lists paths with non-ascii if path is not
            # unicode. However on Linux the server should only deal with
            # bytestreams and posix.listdir does the right thing 
            flist = os.listdir(path)
            for fname in flist:
                attr = SFTPAttributes.from_stat(os.stat(pathjoin(path, fname)))
                attr.filename = fname
                out.append(attr)
            return out
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        path = self._realpath(path)
        try:
            return SFTPAttributes.from_stat(os.stat(path))
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        path = self._realpath(path)
        try:
            return SFTPAttributes.from_stat(os.lstat(path))
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        path = self._realpath(path)
        try:
            if hasattr(os, 'O_BINARY'):
                flags |= os.O_BINARY
            if getattr(attr, 'st_mode', None):
                fd = os.open(path, flags, attr.st_mode)
            else:
                fd = os.open(path, flags)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)

        if (flags & os.O_CREAT) and (attr is not None):
            attr._flags &= ~attr.FLAG_PERMISSIONS
            SFTPServer.set_file_attr(path, attr)
        if flags & os.O_WRONLY:
            fstr = 'wb'
        elif flags & os.O_RDWR:
            fstr = 'rb+'
        else:
            # O_RDONLY (== 0)
            fstr = 'rb'
        try:
            f = os.fdopen(fd, fstr)
        except (IOError, OSError), e:
            return SFTPServer.convert_errno(e.errno)
        fobj = StubSFTPHandle()
        fobj.filename = path
        fobj.readfile = f
        fobj.writefile = f
        return fobj

    def remove(self, path):
        path = self._realpath(path)
        try:
            os.remove(path)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        oldpath = self._realpath(oldpath)
        newpath = self._realpath(newpath)
        try:
            os.rename(oldpath, newpath)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def mkdir(self, path, attr):
        path = self._realpath(path)
        try:
            # Using getattr() in case st_mode is None or 0
            # both evaluate to False
            if getattr(attr, 'st_mode', None):
                os.mkdir(path, attr.st_mode)
            else:
                os.mkdir(path)
            if attr is not None:
                attr._flags &= ~attr.FLAG_PERMISSIONS
                SFTPServer.set_file_attr(path, attr)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path):
        path = self._realpath(path)
        try:
            os.rmdir(path)
        except OSError, e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    # removed: chattr, symlink, readlink
    # (nothing in bzr's sftp transport uses those)
