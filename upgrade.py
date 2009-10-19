# Copyright (C) 2006-2009 by Jelmer Vernooij
# 
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
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


"""Upgrading revisions made with older versions of the mapping."""


import itertools

from bzrlib import (
    trace,
    ui,
    )
from bzrlib.errors import (
    BzrError,
    InvalidRevisionId,
    NoSuchRevision,
    )
from bzrlib.foreign import (
    update_workingtree_fileids,
    )

from bzrlib.plugins.rebase.rebase import (
    generate_transpose_plan,
    replay_snapshot,
    rebase,
    rebase_todo,
    )


class UpgradeChangesContent(BzrError):
    """Inconsistency was found upgrading the mapping of a revision."""
    _fmt = """Upgrade will change contents in revision %(revid)s. Use --allow-changes to override."""

    def __init__(self, revid):
        self.revid = revid


def create_upgraded_revid(revid, mapping_suffix, upgrade_suffix="-upgrade"):
    """Create a new revision id for an upgraded version of a revision.
    
    Prevents suffix to be appended needlessly.

    :param revid: Original revision id.
    :return: New revision id
    """
    if revid.endswith(upgrade_suffix):
        return revid[0:revid.rfind("-svn")] + mapping_suffix + upgrade_suffix
    else:
        return revid + mapping_suffix + upgrade_suffix


def upgrade_workingtree(wt, foreign_repository, new_mapping, 
                        allow_changes=False, verbose=False):
    """Upgrade a working tree.

    :param foreign_repository: Foreign repository object
    """
    wt.lock_write()
    try:
        old_revid = wt.last_revision()
        revid_renames = upgrade_branch(wt.branch, foreign_repository, new_mapping=new_mapping,
                                 allow_changes=allow_changes, verbose=verbose)
        last_revid = wt.branch.last_revision()
        if old_revid == last_revid:
            return revid_renames
        new_tree = wt.branch.repository.revision_tree(last_revid)
        update_workingtree_fileids(wt, new_tree)
    finally:
        wt.unlock()

    return revid_renames


def upgrade_tags(tags, repository, foreign_repository, new_mapping, 
                 allow_changes=False, verbose=False, branch_renames=None,
                 branch_ancestry=None):
    """Upgrade a tags dictionary."""
    renames = {}
    if branch_renames is not None:
        renames.update(branch_renames)
    pb = ui.ui_factory.nested_progress_bar()
    try:
        tags_dict = tags.get_tag_dict()
        for i, (name, revid) in enumerate(tags_dict.iteritems()):
            pb.update("upgrading tags", i, len(tags_dict))
            if not revid in renames:
                renames.update(upgrade_repository(repository, foreign_repository, 
                      revision_id=revid, new_mapping=new_mapping,
                      allow_changes=allow_changes, verbose=verbose))
            if revid in renames and (branch_ancestry is None or not revid in branch_ancestry):
                tags.set_tag(name, renames[revid])
    finally:
        pb.finished()


def upgrade_branch(branch, foreign_repository, new_mapping, 
                   allow_changes=False, verbose=False):
    """Upgrade a branch to the current mapping version.
    
    :param branch: Branch to upgrade.
    :param foreign_repository: Repository to fetch new revisions from
    :param allow_changes: Allow changes in mappings.
    :param verbose: Whether to print verbose list of rewrites
    """
    revid = branch.last_revision()
    renames = upgrade_repository(branch.repository, foreign_repository, 
              revision_id=revid, new_mapping=new_mapping,
              allow_changes=allow_changes, verbose=verbose)
    if revid in renames:
        branch.generate_revision_history(renames[revid])
    ancestry = branch.repository.get_ancestry(branch.last_revision(), topo_sorted=False)
    upgrade_tags(branch.tags, branch.repository, foreign_repository, 
           new_mapping=new_mapping, 
           allow_changes=allow_changes, verbose=verbose, branch_renames=renames,
           branch_ancestry=ancestry)
    return renames


def check_revision_changed(oldrev, newrev):
    """Check if two revisions are different. This is exactly the same 
    as Revision.equals() except that it does not check the revision_id."""
    if (newrev.inventory_sha1 != oldrev.inventory_sha1 or
        newrev.timestamp != oldrev.timestamp or
        newrev.message != oldrev.message or
        newrev.timezone != oldrev.timezone or
        newrev.committer != oldrev.committer or
        newrev.properties != oldrev.properties):
        raise UpgradeChangesContent(oldrev.revision_id)


def generate_upgrade_map(revs, vcs, determine_upgraded_revid):
    """Generate an upgrade map for use by bzr-rebase.

    :param new_mapping: Mapping to upgrade revisions to.
    :param vcs: The foreign vcs
    :param revs: Iterator over revisions to upgrade.
    :return: Map from old revids as keys, new revids as values stored in a 
             dictionary.
    """
    rename_map = {}
    # Create a list of revisions that can be renamed during the upgrade
    for revid in revs:
        assert isinstance(revid, str)
        try:
            (foreign_revid, old_mapping) = \
                vcs.mapping_registry.parse_revision_id(revid)
        except InvalidRevisionId:
            # Not a foreign revision, nothing to do
            continue
        newrevid = determine_upgraded_revid(foreign_revid)
        if newrevid in (revid, None):
            continue
        rename_map[revid] = newrevid
    return rename_map


def create_upgrade_plan(repository, foreign_repository, new_mapping,
                        revision_id=None, allow_changes=False):
    """Generate a rebase plan for upgrading revisions.

    :param repository: Repository to do upgrade in
    :param foreign_repository: Subversion repository to fetch new revisions from.
    :param new_mapping: New mapping to use.
    :param revision_id: Revision to upgrade (None for all revisions in 
        repository.)
    :param allow_changes: Whether an upgrade is allowed to change the contents
        of revisions.
    :return: Tuple with a rebase plan and map of renamed revisions.
    """

    graph = repository.get_graph()
    if revision_id is None:
        potential = repository.all_revision_ids()
    else:
        potential = itertools.imap(lambda (rev, parents): rev, 
                graph.iter_ancestry([revision_id]))

    def determine_upgraded_revid(foreign_revid):
        # FIXME: Try all mappings until new_mapping rather than just new_mapping
        new_revid = foreign_repository.upgrade_foreign_revision_id(foreign_revid, new_mapping)
        if new_revid is None:
            return None
        # Make sure the revision is there
        if not repository.has_revision(new_revid):
            try:
                repository.fetch(foreign_repository, new_revid)
            except NoSuchRevision:
                return None
            if not repository.has_revision(new_revid):
                return None
        return new_revid

    upgrade_map = generate_upgrade_map(potential, foreign_repository.vcs, 
                                       determine_upgraded_revid)
   
    if not allow_changes:
        for oldrevid, newrevid in upgrade_map.iteritems():
            oldrev = repository.get_revision(oldrevid)
            newrev = repository.get_revision(newrevid)
            check_revision_changed(oldrev, newrev)

    if revision_id is None:
        heads = repository.all_revision_ids() 
    else:
        heads = [revision_id]

    def determine_new_revid(old_revid):
        # If this revision id already exists round-tripped upstream, 
        # leave it alone.
        if foreign_repository.has_revision(old_revid):
            return old_revid
        # if not, return old_revid'
        return create_upgraded_revid(old_revid, new_mapping.upgrade_suffix)

    plan = generate_transpose_plan(graph.iter_ancestry(heads), upgrade_map, 
      graph, determine_new_revid)
    def remove_parents((oldrevid, (newrevid, parents))):
        return (oldrevid, newrevid)
    upgrade_map.update(dict(map(remove_parents, plan.iteritems())))

    return (plan, upgrade_map)

 
def upgrade_repository(repository, foreign_repository, new_mapping, 
                       revision_id=None, allow_changes=False, 
                       verbose=False):
    """Upgrade the revisions in repository until the specified stop revision.

    :param repository: Repository in which to upgrade.
    :param foreign_repository: Repository to fetch new revisions from.
    :param new_mapping: New mapping.
    :param revision_id: Revision id up until which to upgrade, or None for 
                        all revisions.
    :param allow_changes: Allow changes to mappings.
    :param verbose: Whether to print list of rewrites
    :return: Dictionary of mapped revisions
    """

    # Find revisions that need to be upgraded, create
    # dictionary with revision ids in key, new parents in value
    try:
        repository.lock_write()
        foreign_repository.lock_read()
        (plan, revid_renames) = create_upgrade_plan(repository,
            foreign_repository, new_mapping, revision_id=revision_id,
            allow_changes=allow_changes)
        if verbose:
            for revid in rebase_todo(repository, plan):
                trace.note("%s -> %s" % (revid, plan[revid][0]))
        rebase(repository, plan, replay_snapshot)
        return revid_renames
    finally:
        repository.unlock()
        foreign_repository.unlock()

