# Copyright (C) 2007 Canonical Ltd
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

"""Tests for Branch.sprout()"""

import os
from bzrlib import (
    branch as _mod_branch,
    errors,
    remote,
    revision as _mod_revision,
    tests,
    )
from bzrlib.tests import KnownFailure, SymlinkFeature, UnicodeFilenameFeature
from bzrlib.tests.branch_implementations import TestCaseWithBranch


class TestSprout(TestCaseWithBranch):

    def test_sprout_branch_nickname(self):
        # test the nick name is reset always
        raise tests.TestSkipped('XXX branch sprouting is not yet tested.')

    def test_sprout_branch_parent(self):
        source = self.make_branch('source')
        target = source.bzrdir.sprout(self.get_url('target')).open_branch()
        self.assertEqual(source.bzrdir.root_transport.base, target.get_parent())

    def test_sprout_uses_bzrdir_branch_format(self):
        # branch.sprout(bzrdir) is defined as using the branch format selected
        # by bzrdir; format preservation is achieved by parameterising the
        # bzrdir during bzrdir.sprout, which is where stacking compatibility
        # checks are done. So this test tests that each implementation of
        # Branch.sprout delegates appropriately to the bzrdir which the
        # branch is being created in, rather than testing that the result is
        # in the format that we are testing (which is what would happen if
        # the branch did not delegate appropriately).
        if isinstance(self.branch_format, _mod_branch.BranchReferenceFormat):
            raise tests.TestNotApplicable('cannot sprout to a reference')
        # Start with a format that is unlikely to be the target format
        # We call the super class to allow overriding the format of creation)
        source = tests.TestCaseWithTransport.make_branch(self, 'old-branch',
                                                         format='metaweave')
        target_bzrdir = self.make_bzrdir('target')
        target_bzrdir.create_repository()
        result_format = self.branch_format
        if isinstance(target_bzrdir, remote.RemoteBzrDir):
            # for a remote bzrdir, we need to parameterise it with a branch
            # format, as, after creation, the newly opened remote objects
            # do not have one unless a branch was created at the time.
            # We use branch format 6 because its not the default, and its not
            # metaweave either.
            target_bzrdir._format.set_branch_format(_mod_branch.BzrBranchFormat6())
            result_format = target_bzrdir._format.get_branch_format()
        target = source.sprout(target_bzrdir)
        if isinstance(target, remote.RemoteBranch):
            # we have to look at the real branch to see whether RemoteBranch
            # did the right thing.
            target._ensure_real()
            target = target._real_branch
        if isinstance(result_format, remote.RemoteBranchFormat):
            # Unwrap a parameterised RemoteBranchFormat for comparison.
            result_format = result_format._custom_format
        self.assertIs(result_format.__class__, target._format.__class__)

    def test_sprout_partial(self):
        # test sprouting with a prefix of the revision-history.
        # also needs not-on-revision-history behaviour defined.
        wt_a = self.make_branch_and_tree('a')
        self.build_tree(['a/one'])
        wt_a.add(['one'])
        wt_a.commit('commit one', rev_id='1')
        self.build_tree(['a/two'])
        wt_a.add(['two'])
        wt_a.commit('commit two', rev_id='2')
        repo_b = self.make_repository('b')
        repo_a = wt_a.branch.repository
        repo_a.copy_content_into(repo_b)
        br_b = wt_a.branch.sprout(repo_b.bzrdir, revision_id='1')
        self.assertEqual('1', br_b.last_revision())

    def test_sprout_partial_not_in_revision_history(self):
        """We should be able to sprout from any revision in ancestry."""
        wt = self.make_branch_and_tree('source')
        self.build_tree(['source/a'])
        wt.add('a')
        wt.commit('rev1', rev_id='rev1')
        wt.commit('rev2-alt', rev_id='rev2-alt')
        wt.set_parent_ids(['rev1'])
        wt.branch.set_last_revision_info(1, 'rev1')
        wt.commit('rev2', rev_id='rev2')
        wt.set_parent_ids(['rev2', 'rev2-alt'])
        wt.commit('rev3', rev_id='rev3')

        repo = self.make_repository('target')
        repo.fetch(wt.branch.repository)
        branch2 = wt.branch.sprout(repo.bzrdir, revision_id='rev2-alt')
        self.assertEqual((2, 'rev2-alt'), branch2.last_revision_info())
        self.assertEqual(['rev1', 'rev2-alt'], branch2.revision_history())

    def test_sprout_from_any_repo_revision(self):
        """We should be able to sprout from any revision."""
        wt = self.make_branch_and_tree('source')
        self.build_tree(['source/a'])
        wt.add('a')
        wt.commit('rev1a', rev_id='rev1a')
        # simulated uncommit
        wt.branch.set_last_revision_info(0, _mod_revision.NULL_REVISION)
        wt.set_last_revision(_mod_revision.NULL_REVISION)
        wt.revert()
        wt.commit('rev1b', rev_id='rev1b')
        wt2 = wt.bzrdir.sprout('target',
            revision_id='rev1a').open_workingtree()
        self.assertEqual('rev1a', wt2.last_revision())
        self.failUnlessExists('target/a')

    def test_sprout_with_unicode_symlink(self):
        # this tests bug #272444
        # Since the trigger function seems to be set_parent_trees, there exists
        # also a similar test, with name test_unicode_symlink, in class
        # TestSetParents at file workingtree_implementations/test_parents.py
        self.requireFeature(SymlinkFeature)
        self.requireFeature(UnicodeFilenameFeature)

        tree = self.make_branch_and_tree('tree1')

        # The link points to a file whose name is an omega
        # U+03A9 GREEK CAPITAL LETTER OMEGA
        # UTF-8: ce a9  UTF-16BE: 03a9  Decimal: &#937;
        os.symlink(u'\u03a9','tree1/link_name')
        tree.add(['link_name'],['link-id'])

        try:
            # python 2.7a0 failed on commit:
            revision = tree.commit('added a link to a Unicode target')
            # python 2.5 failed on sprout:
            tree.bzrdir.sprout('target')
        except UnicodeEncodeError, e:
            raise KnownFailure('there is no support for'
                               ' symlinks to non-ASCII targets (bug #272444)')

    def assertBranchHookBranchIsStacked(self, pre_change_params):
        # Just calling will either succeed or fail.
        pre_change_params.branch.get_stacked_on_url()
        self.hook_calls.append(pre_change_params)

    def test_sprout_stacked_hooks_get_stacked_branch(self):
        tree = self.make_branch_and_tree('source')
        tree.commit('a commit')
        revid = tree.commit('a second commit')
        source = tree.branch
        target_transport = self.get_transport('target')
        self.hook_calls = []
        _mod_branch.Branch.hooks.install_named_hook("pre_change_branch_tip",
            self.assertBranchHookBranchIsStacked, None)
        try:
            dir = source.bzrdir.sprout(target_transport.base,
                source.last_revision(), possible_transports=[target_transport],
                source_branch=source, stacked=True)
        except errors.UnstackableBranchFormat:
            if isinstance(self.branch_format, _mod_branch.BzrBranchFormat4):
                raise KnownFailure("Format 4 doesn't auto stack successfully.")
            else:
                raise
        result = dir.open_branch()
        self.assertEqual(revid, result.last_revision())
        self.assertEqual(source.base, result.get_stacked_on_url())
        # Smart servers invoke hooks on both sides
        if isinstance(result, remote.RemoteBranch):
            expected_calls = 2
        else:
            expected_calls = 1
        self.assertEqual(expected_calls, len(self.hook_calls))

