# Copyright (C) 2005, 2007, 2008, 2009 Canonical Ltd
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


"""Black-box tests for bzr dpush."""


import os

from bzrlib import (
    branch,
    bzrdir,
    foreign,
    tests,
    workingtree,
    )
from bzrlib.tests import (
    blackbox,
    test_foreign,
    )
from bzrlib.tests.blackbox import test_push


def load_tests(standard_tests, module, loader):
    """Multiply tests for the dpush command."""
    result = loader.suiteClass()

    # one for each king of change
    changes_tests, remaining_tests = tests.split_suite_by_condition(
        standard_tests, tests.condition_isinstance((
                TestDpushStrictWithChanges,
                )))
    changes_scenarios = [
        ('uncommitted',
         dict(_changes_type= '_uncommitted_changes')),
        ('pending-merges',
         dict(_changes_type= '_pending_merges')),
        ('out-of-sync-trees',
         dict(_changes_type= '_out_of_sync_trees')),
        ]
    tests.multiply_tests(changes_tests, changes_scenarios, result)
    # No parametrization for the remaining tests
    result.addTests(remaining_tests)

    return result


class TestDpush(blackbox.ExternalBase):

    def setUp(self):
        super(TestDpush, self).setUp()
        test_foreign.register_dummy_foreign_for_test(self)

    def make_dummy_builder(self, relpath):
        builder = self.make_branch_builder(
            relpath, format=test_foreign.DummyForeignVcsDirFormat())
        builder.build_snapshot('revid', None,
            [('add', ('', 'TREE_ROOT', 'directory', None)),
             ('add', ('foo', 'fooid', 'file', 'bar'))])
        return builder

    def test_dpush_native(self):
        target_tree = self.make_branch_and_tree("dp")
        source_tree = self.make_branch_and_tree("dc")
        output, error = self.run_bzr("dpush -d dc dp", retcode=3)
        self.assertEquals("", output)
        self.assertContainsRe(error, 'in the same VCS, lossy push not necessary. Please use regular push.')

    def test_dpush(self):
        branch = self.make_dummy_builder('d').get_branch()

        dc = branch.bzrdir.sprout('dc', force_new_repo=True)
        self.build_tree(("dc/foo", "blaaaa"))
        dc.open_workingtree().commit('msg')

        output, error = self.run_bzr("dpush -d dc d")
        self.assertEquals(error, "Pushed up to revision 2.\n")
        self.check_output("", "status dc")

    def test_dpush_new(self):
        b = self.make_dummy_builder('d').get_branch()

        dc = b.bzrdir.sprout('dc', force_new_repo=True)
        self.build_tree_contents([("dc/foofile", "blaaaa")])
        dc_tree = dc.open_workingtree()
        dc_tree.add("foofile")
        dc_tree.commit("msg")

        self.check_output("", "dpush -d dc d")
        self.check_output("2\n", "revno dc")
        self.check_output("", "status dc")

    def test_dpush_wt_diff(self):
        b = self.make_dummy_builder('d').get_branch()

        dc = b.bzrdir.sprout('dc', force_new_repo=True)
        self.build_tree_contents([("dc/foofile", "blaaaa")])
        dc_tree = dc.open_workingtree()
        dc_tree.add("foofile")
        newrevid = dc_tree.commit('msg')

        self.build_tree_contents([("dc/foofile", "blaaaal")])
        self.check_output("", "dpush -d dc d --no-strict")
        self.assertFileEqual("blaaaal", "dc/foofile")
        # if the dummy vcs wasn't that dummy we could uncomment the line below
        # self.assertFileEqual("blaaaa", "d/foofile")
        self.check_output('modified:\n  foofile\n', "status dc")

    def test_diverged(self):
        builder = self.make_dummy_builder('d')

        b = builder.get_branch()

        dc = b.bzrdir.sprout('dc', force_new_repo=True)
        dc_tree = dc.open_workingtree()

        self.build_tree_contents([("dc/foo", "bar")])
        dc_tree.commit('msg1')

        builder.build_snapshot('revid2', None,
          [('modify', ('fooid', 'blie'))])

        output, error = self.run_bzr("dpush -d dc d", retcode=3)
        self.assertEquals(output, "")
        self.assertContainsRe(error, "have diverged")


class TestDpushStrictMixin(object):

    def setUp(self):
        test_foreign.register_dummy_foreign_for_test(self)
        # Create an empty branch where we will be able to push
        self.foreign = self.make_branch(
            'to', format=test_foreign.DummyForeignVcsDirFormat())

    def set_config_push_strict(self, value):
        # set config var (any of bazaar.conf, locations.conf, branch.conf
        # should do)
        conf = self.tree.branch.get_config()
        conf.set_user_option('dpush_strict', value)

    _default_command = ['dpush', '../to']
    _default_pushed_revid = False # Doesn't aplly for dpush

    def assertPushSucceeds(self, args, pushed_revid=None):
        self.run_bzr(self._default_command + args,
                     working_dir=self._default_wd)
        if pushed_revid is None:
            # dpush change the revids, so we need to get back to it
            branch_from = branch.Branch.open(self._default_wd)
            pushed_revid = branch_from.last_revision()
        branch_to = branch.Branch.open('to')
        repo_to = branch_to.repository
        self.assertTrue(repo_to.has_revision(pushed_revid))
        self.assertEqual(branch_to.last_revision(), pushed_revid)



class TestDpushStrictWithoutChanges(TestDpushStrictMixin,
                                    test_push.TestPushStrictWithoutChanges):

    def setUp(self):
        test_push.TestPushStrictWithoutChanges.setUp(self)
        TestDpushStrictMixin.setUp(self)


class TestDpushStrictWithChanges(TestDpushStrictMixin,
                                 test_push.TestPushStrictWithChanges):

    _changes_type = None # Set by load_tests

    def setUp(self):
        test_push.TestPushStrictWithChanges.setUp(self)
        TestDpushStrictMixin.setUp(self)

    def test_push_with_revision(self):
        raise tests.TestNotApplicable('dpush does not handle --revision')
