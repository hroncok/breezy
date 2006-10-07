# Copyright (C) 2006 by Canonical Ltd
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

from bzrlib import bzrdir, repository, tests, workingtree


class TestJoin(tests.TestCaseWithTransport):

    def make_trees(self):
        format = bzrdir.get_knit2_format()
        base_tree = self.make_branch_and_tree('tree', format=format)
        base_tree.commit('empty commit')
        self.build_tree(['tree/subtree/', 'tree/subtree/file1'])
        sub_tree = self.make_branch_and_tree('tree/subtree')
        sub_tree.add('file1', 'file1-id')
        sub_tree.commit('added file1')
        return base_tree, sub_tree

    def check_success(self, path):
        base_tree = workingtree.WorkingTree.open(path)
        self.assertEqual('file1-id', base_tree.path2id('subtree/file1'))

    def test_join(self):
        base_tree, sub_tree = self.make_trees()
        self.run_bzr('join', 'tree/subtree')
        self.check_success('tree')

    def test_join_dot(self):
        base_tree, sub_tree = self.make_trees()
        self.run_bzr('join', '.', working_dir='tree/subtree')
        self.check_success('tree')

    def test_join_error(self):
        base_tree, sub_tree = self.make_trees()
        os.mkdir('tree/subtree2')
        os.rename('tree/subtree', 'tree/subtree2/subtree')
        self.run_bzr_error(('Cannot join .*subtree.  Parent directory is not'
                            ' versioned',), 'join', 'tree/subtree2/subtree')
        self.run_bzr_error(('Not a branch:.*subtree2',), 'join', 
                            'tree/subtree2')
