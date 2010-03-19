# Copyright (C) 2006, 2008, 2009, 2010 Canonical Ltd
# Authors:  Robert Collins <robert.collins@canonical.com>
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Tests for the RevisionTree class."""

from bzrlib import (
    errors,
    revision,
    )
import bzrlib
from bzrlib.inventory import ROOT_ID
from bzrlib.tests import TestCaseWithTransport


class TestTreeWithCommits(TestCaseWithTransport):

    def setUp(self):
        super(TestTreeWithCommits, self).setUp()
        self.t = self.make_branch_and_tree('.')
        self.rev_id = self.t.commit('foo', allow_pointless=True)
        self.rev_tree = self.t.branch.repository.revision_tree(self.rev_id)

    def test_empty_no_unknowns(self):
        self.assertEqual([], list(self.rev_tree.unknowns()))

    def test_no_conflicts(self):
        self.assertEqual([], list(self.rev_tree.conflicts()))

    def test_parents(self):
        """RevisionTree.parent_ids should match the revision graph."""
        # XXX: TODO: Should this be a repository_implementation test ?
        # at the end of the graph, we get []
        self.assertEqual([], self.rev_tree.get_parent_ids())
        # do a commit to look further up
        revid_2 = self.t.commit('bar', allow_pointless=True)
        self.assertEqual(
            [self.rev_id],
            self.t.branch.repository.revision_tree(revid_2).get_parent_ids())
        # TODO commit a merge and check it is reported correctly.

        # the parents for a revision_tree(NULL_REVISION) are []:
        self.assertEqual([],
            self.t.branch.repository.revision_tree(
                revision.NULL_REVISION).get_parent_ids())

    def test_empty_no_root(self):
        null_tree = self.t.branch.repository.revision_tree(
            revision.NULL_REVISION)
        self.assertIs(None, null_tree.inventory.root)

    def test_get_file_mtime_ghost(self):
        file_id = iter(self.rev_tree).next()
        self.rev_tree.inventory[file_id].revision = 'ghostrev'
        self.assertRaises(errors.FileTimestampUnavailable, 
            self.rev_tree.get_file_mtime, file_id)