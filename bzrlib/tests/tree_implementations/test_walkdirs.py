# Copyright (C) 2006, 2007 Canonical Ltd
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

"""Tests for the generic Tree.walkdirs interface."""

from bzrlib.osutils import has_symlinks
from bzrlib.tests.tree_implementations import TestCaseWithTree


class TestWalkdirs(TestCaseWithTree):

    def get_all_subdirs_expected(self, tree, symlinks):
        if symlinks:
            return [
                (('', tree.path2id('')),
                [
                 ('0file', '0file', 'file', None, '2file', 'file'),
                 ('1top-dir', '1top-dir', 'directory', None, '1top-dir', 'directory'),
                 (u'2utf\u1234file', u'2utf\u1234file', 'file', None,
                                         u'0utf\u1234file'.encode('utf8'), 'file'),
                 ('symlink', 'symlink', 'symlink', None, 'symlink', 'symlink')
                ]),
                (('1top-dir', '1top-dir'),
                [('1top-dir/0file-in-1topdir', '0file-in-1topdir', 'file', None, '1file-in-1topdir', 'file'),
                 ('1top-dir/1dir-in-1topdir', '1dir-in-1topdir', 'directory', None, '0dir-in-1topdir', 'directory'),
                ]),
                (('1top-dir/1dir-in-1topdir', '0dir-in-1topdir'),
                [
                ]),
                ]
        else:
            return [
                (('', tree.path2id('')),
                [
                 ('0file', '0file', 'file', None, '2file', 'file'),
                 ('1top-dir', '1top-dir', 'directory', None, '1top-dir', 'directory'),
                 (u'2utf\u1234file', u'2utf\u1234file', 'file', None,
                                         u'0utf\u1234file'.encode('utf8'), 'file'),
                ]),
                (('1top-dir', '1top-dir'),
                [('1top-dir/0file-in-1topdir', '0file-in-1topdir', 'file', None, '1file-in-1topdir', 'file'),
                 ('1top-dir/1dir-in-1topdir', '1dir-in-1topdir', 'directory', None, '0dir-in-1topdir', 'directory'),
                ]),
                (('1top-dir/1dir-in-1topdir', '0dir-in-1topdir'),
                [
                ]),
                ]

    def test_walkdir_root(self):

        import sys
        from bzrlib.tests import KnownFailure
        from bzrlib.tests.tree_implementations import _dirstate_tree_from_workingtree
        if (self.workingtree_to_test_tree is _dirstate_tree_from_workingtree
            and sys.version_info >= (2, 6)):
            # Furthermore the problem here is that it interacts badly with a
            # '\0\n\0'.join(lines) where lines contains *one* unicode string
            # where all the other strings are not unicode.
            raise  KnownFailure("python-2.6 os.readlink returns unicode path"
                                " if called with unicode path")

        tree = self.get_tree_with_subdirs_and_all_supported_content_types(has_symlinks())
        tree.lock_read()
        expected_dirblocks = self.get_all_subdirs_expected(tree, has_symlinks())
        # test that its iterable by iterating
        result = []
        for dirinfo, block in tree.walkdirs():
            newblock = []
            for row in block:
                if row[4] is not None:
                    newblock.append(row[0:3] + (None,) + row[4:])
                else:
                    newblock.append(row)
            result.append((dirinfo, newblock))
        tree.unlock()
        # check each return value for debugging ease.
        for pos, item in enumerate(expected_dirblocks):
            self.assertEqual(item, result[pos])
        self.assertEqual(len(expected_dirblocks), len(result))
            
    def test_walkdir_subtree(self):

        import sys
        from bzrlib.tests import KnownFailure
        from bzrlib.tests.tree_implementations import _dirstate_tree_from_workingtree
        if (self.workingtree_to_test_tree is _dirstate_tree_from_workingtree
            and sys.version_info >= (2, 6)):
            # Furthermore the problem here is that it interacts badly with a
            # '\0\n\0'.join(lines) where lines contains *one* unicode string
            # where all the other strings are not unicode.
            raise  KnownFailure("python-2.6 os.readlink returns unicode path"
                                " if called with unicode path")

        tree = self.get_tree_with_subdirs_and_all_supported_content_types(has_symlinks())
        # test that its iterable by iterating
        result = []
        tree.lock_read()
        expected_dirblocks = self.get_all_subdirs_expected(tree, has_symlinks())[1:]
        for dirinfo, block in tree.walkdirs('1top-dir'):
            newblock = []
            for row in block:
                if row[4] is not None:
                    newblock.append(row[0:3] + (None,) + row[4:])
                else:
                    newblock.append(row)
            result.append((dirinfo, newblock))
        tree.unlock()
        # check each return value for debugging ease.
        for pos, item in enumerate(expected_dirblocks):
            self.assertEqual(item, result[pos])
        self.assertEqual(len(expected_dirblocks), len(result))
