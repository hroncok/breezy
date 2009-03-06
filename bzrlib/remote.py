# Copyright (C) 2006, 2007, 2008 Canonical Ltd
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

# TODO: At some point, handle upgrades by just passing the whole request
# across to run on the server.

import bz2

from bzrlib import (
    branch,
    bzrdir,
    debug,
    errors,
    graph,
    lockdir,
    pack,
    repository,
    revision,
    symbol_versioning,
    urlutils,
)
from bzrlib.branch import BranchReferenceFormat
from bzrlib.bzrdir import BzrDir, RemoteBzrDirFormat
from bzrlib.decorators import needs_read_lock, needs_write_lock
from bzrlib.errors import (
    NoSuchRevision,
    SmartProtocolError,
    )
from bzrlib.lockable_files import LockableFiles
from bzrlib.smart import client, vfs, repository as smart_repo
from bzrlib.revision import ensure_null, NULL_REVISION
from bzrlib.trace import mutter, note, warning
from bzrlib.util import bencode


class _RpcHelper(object):
    """Mixin class that helps with issuing RPCs."""

    def _call(self, method, *args, **err_context):
        try:
            return self._client.call(method, *args)
        except errors.ErrorFromSmartServer, err:
            self._translate_error(err, **err_context)

    def _call_expecting_body(self, method, *args, **err_context):
        try:
            return self._client.call_expecting_body(method, *args)
        except errors.ErrorFromSmartServer, err:
            self._translate_error(err, **err_context)

    def _call_with_body_bytes_expecting_body(self, method, args, body_bytes,
                                             **err_context):
        try:
            return self._client.call_with_body_bytes_expecting_body(
                method, args, body_bytes)
        except errors.ErrorFromSmartServer, err:
            self._translate_error(err, **err_context)


def response_tuple_to_repo_format(response):
    """Convert a response tuple describing a repository format to a format."""
    format = RemoteRepositoryFormat()
    format.rich_root_data = (response[0] == 'yes')
    format.supports_tree_reference = (response[1] == 'yes')
    format.supports_external_lookups = (response[2] == 'yes')
    format._network_name = response[3]
    return format


# Note: RemoteBzrDirFormat is in bzrdir.py

class RemoteBzrDir(BzrDir, _RpcHelper):
    """Control directory on a remote server, accessed via bzr:// or similar."""

    def __init__(self, transport, format, _client=None):
        """Construct a RemoteBzrDir.

        :param _client: Private parameter for testing. Disables probing and the
            use of a real bzrdir.
        """
        BzrDir.__init__(self, transport, format)
        # this object holds a delegated bzrdir that uses file-level operations
        # to talk to the other side
        self._real_bzrdir = None
        # 1-shot cache for the call pattern 'create_branch; open_branch' - see
        # create_branch for details.
        self._next_open_branch_result = None

        if _client is None:
            medium = transport.get_smart_medium()
            self._client = client._SmartClient(medium)
        else:
            self._client = _client
            return

        path = self._path_for_remote_call(self._client)
        response = self._call('BzrDir.open', path)
        if response not in [('yes',), ('no',)]:
            raise errors.UnexpectedSmartServerResponse(response)
        if response == ('no',):
            raise errors.NotBranchError(path=transport.base)

    def _ensure_real(self):
        """Ensure that there is a _real_bzrdir set.

        Used before calls to self._real_bzrdir.
        """
        if not self._real_bzrdir:
            self._real_bzrdir = BzrDir.open_from_transport(
                self.root_transport, _server_formats=False)
            self._format._network_name = \
                self._real_bzrdir._format.network_name()

    def _translate_error(self, err, **context):
        _translate_error(err, bzrdir=self, **context)

    def break_lock(self):
        # Prevent aliasing problems in the next_open_branch_result cache.
        # See create_branch for rationale.
        self._next_open_branch_result = None
        return BzrDir.break_lock(self)

    def _vfs_cloning_metadir(self, require_stacking=False):
        self._ensure_real()
        return self._real_bzrdir.cloning_metadir(
            require_stacking=require_stacking)

    def cloning_metadir(self, require_stacking=False):
        medium = self._client._medium
        if medium._is_remote_before((1, 13)):
            return self._vfs_cloning_metadir(require_stacking=require_stacking)
        verb = 'BzrDir.cloning_metadir'
        if require_stacking:
            stacking = 'True'
        else:
            stacking = 'False'
        path = self._path_for_remote_call(self._client)
        try:
            response = self._call(verb, path, stacking)
        except errors.UnknownSmartMethod:
            return self._vfs_cloning_metadir(require_stacking=require_stacking)
        if len(response) != 3:
            raise errors.UnexpectedSmartServerResponse(response)
        control_name, repo_name, branch_info = response
        if len(branch_info) != 2:
            raise errors.UnexpectedSmartServerResponse(response)
        branch_ref, branch_name = branch_info
        format = bzrdir.network_format_registry.get(control_name)
        if repo_name:
            format.repository_format = repository.network_format_registry.get(
                repo_name)
        if branch_ref == 'reference':
            # XXX: we need possible_transports here to avoid reopening the
            # connection to the referenced location
            ref_bzrdir = BzrDir.open(branch_name)
            branch_format = ref_bzrdir.cloning_metadir().get_branch_format()
            format.set_branch_format(branch_format)
        elif branch_ref == 'direct':
            if branch_name:
                format.set_branch_format(
                    branch.network_format_registry.get(branch_name))
        else:
            raise errors.UnexpectedSmartServerResponse(response)
        return format

    def create_repository(self, shared=False):
        # as per meta1 formats - just delegate to the format object which may
        # be parameterised.
        result = self._format.repository_format.initialize(self, shared)
        if not isinstance(result, RemoteRepository):
            return self.open_repository()
        else:
            return result

    def destroy_repository(self):
        """See BzrDir.destroy_repository"""
        self._ensure_real()
        self._real_bzrdir.destroy_repository()

    def create_branch(self):
        # as per meta1 formats - just delegate to the format object which may
        # be parameterised.
        real_branch = self._format.get_branch_format().initialize(self)
        if not isinstance(real_branch, RemoteBranch):
            result = RemoteBranch(self, self.find_repository(), real_branch)
        else:
            result = real_branch
        # BzrDir.clone_on_transport() uses the result of create_branch but does
        # not return it to its callers; we save approximately 8% of our round
        # trips by handing the branch we created back to the first caller to
        # open_branch rather than probing anew. Long term we need a API in
        # bzrdir that doesn't discard result objects (like result_branch).
        # RBC 20090225
        self._next_open_branch_result = result
        return result

    def destroy_branch(self):
        """See BzrDir.destroy_branch"""
        self._ensure_real()
        self._real_bzrdir.destroy_branch()
        self._next_open_branch_result = None

    def create_workingtree(self, revision_id=None, from_branch=None):
        raise errors.NotLocalUrl(self.transport.base)

    def find_branch_format(self):
        """Find the branch 'format' for this bzrdir.

        This might be a synthetic object for e.g. RemoteBranch and SVN.
        """
        b = self.open_branch()
        return b._format

    def get_branch_reference(self):
        """See BzrDir.get_branch_reference()."""
        path = self._path_for_remote_call(self._client)
        response = self._call('BzrDir.open_branch', path)
        if response[0] == 'ok':
            if response[1] == '':
                # branch at this location.
                return None
            else:
                # a branch reference, use the existing BranchReference logic.
                return response[1]
        else:
            raise errors.UnexpectedSmartServerResponse(response)

    def _get_tree_branch(self):
        """See BzrDir._get_tree_branch()."""
        return None, self.open_branch()

    def open_branch(self, _unsupported=False):
        if _unsupported:
            raise NotImplementedError('unsupported flag support not implemented yet.')
        if self._next_open_branch_result is not None:
            # See create_branch for details.
            result = self._next_open_branch_result
            self._next_open_branch_result = None
            return result
        reference_url = self.get_branch_reference()
        if reference_url is None:
            # branch at this location.
            return RemoteBranch(self, self.find_repository())
        else:
            # a branch reference, use the existing BranchReference logic.
            format = BranchReferenceFormat()
            return format.open(self, _found=True, location=reference_url)

    def _open_repo_v1(self, path):
        verb = 'BzrDir.find_repository'
        response = self._call(verb, path)
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        # servers that only support the v1 method don't support external
        # references either.
        self._ensure_real()
        repo = self._real_bzrdir.open_repository()
        response = response + ('no', repo._format.network_name())
        return response, repo

    def _open_repo_v2(self, path):
        verb = 'BzrDir.find_repositoryV2'
        response = self._call(verb, path)
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        self._ensure_real()
        repo = self._real_bzrdir.open_repository()
        response = response + (repo._format.network_name(),)
        return response, repo

    def _open_repo_v3(self, path):
        verb = 'BzrDir.find_repositoryV3'
        medium = self._client._medium
        if medium._is_remote_before((1, 13)):
            raise errors.UnknownSmartMethod(verb)
        response = self._call(verb, path)
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        return response, None

    def open_repository(self):
        path = self._path_for_remote_call(self._client)
        response = None
        for probe in [self._open_repo_v3, self._open_repo_v2,
            self._open_repo_v1]:
            try:
                response, real_repo = probe(path)
                break
            except errors.UnknownSmartMethod:
                pass
        if response is None:
            raise errors.UnknownSmartMethod('BzrDir.find_repository{3,2,}')
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        if len(response) != 6:
            raise SmartProtocolError('incorrect response length %s' % (response,))
        if response[1] == '':
            # repo is at this dir.
            format = response_tuple_to_repo_format(response[2:])
            # Used to support creating a real format instance when needed.
            format._creating_bzrdir = self
            remote_repo = RemoteRepository(self, format)
            format._creating_repo = remote_repo
            if real_repo is not None:
                remote_repo._set_real_repository(real_repo)
            return remote_repo
        else:
            raise errors.NoRepositoryPresent(self)

    def open_workingtree(self, recommend_upgrade=True):
        self._ensure_real()
        if self._real_bzrdir.has_workingtree():
            raise errors.NotLocalUrl(self.root_transport)
        else:
            raise errors.NoWorkingTree(self.root_transport.base)

    def _path_for_remote_call(self, client):
        """Return the path to be used for this bzrdir in a remote call."""
        return client.remote_path_from_transport(self.root_transport)

    def get_branch_transport(self, branch_format):
        self._ensure_real()
        return self._real_bzrdir.get_branch_transport(branch_format)

    def get_repository_transport(self, repository_format):
        self._ensure_real()
        return self._real_bzrdir.get_repository_transport(repository_format)

    def get_workingtree_transport(self, workingtree_format):
        self._ensure_real()
        return self._real_bzrdir.get_workingtree_transport(workingtree_format)

    def can_convert_format(self):
        """Upgrading of remote bzrdirs is not supported yet."""
        return False

    def needs_format_conversion(self, format=None):
        """Upgrading of remote bzrdirs is not supported yet."""
        if format is None:
            symbol_versioning.warn(symbol_versioning.deprecated_in((1, 13, 0))
                % 'needs_format_conversion(format=None)')
        return False

    def clone(self, url, revision_id=None, force_new_repo=False,
              preserve_stacking=False):
        self._ensure_real()
        return self._real_bzrdir.clone(url, revision_id=revision_id,
            force_new_repo=force_new_repo, preserve_stacking=preserve_stacking)

    def get_config(self):
        self._ensure_real()
        return self._real_bzrdir.get_config()


class RemoteRepositoryFormat(repository.RepositoryFormat):
    """Format for repositories accessed over a _SmartClient.

    Instances of this repository are represented by RemoteRepository
    instances.

    The RemoteRepositoryFormat is parameterized during construction
    to reflect the capabilities of the real, remote format. Specifically
    the attributes rich_root_data and supports_tree_reference are set
    on a per instance basis, and are not set (and should not be) at
    the class level.

    :ivar _custom_format: If set, a specific concrete repository format that
        will be used when initializing a repository with this
        RemoteRepositoryFormat.
    :ivar _creating_repo: If set, the repository object that this
        RemoteRepositoryFormat was created for: it can be called into
        to obtain data like the network name.
    """

    _matchingbzrdir = RemoteBzrDirFormat()

    def __init__(self):
        repository.RepositoryFormat.__init__(self)
        self._custom_format = None
        self._network_name = None
        self._creating_bzrdir = None

    def _vfs_initialize(self, a_bzrdir, shared):
        """Helper for common code in initialize."""
        if self._custom_format:
            # Custom format requested
            result = self._custom_format.initialize(a_bzrdir, shared=shared)
        elif self._creating_bzrdir is not None:
            # Use the format that the repository we were created to back
            # has.
            prior_repo = self._creating_bzrdir.open_repository()
            prior_repo._ensure_real()
            result = prior_repo._real_repository._format.initialize(
                a_bzrdir, shared=shared)
        else:
            # assume that a_bzr is a RemoteBzrDir but the smart server didn't
            # support remote initialization.
            # We delegate to a real object at this point (as RemoteBzrDir
            # delegate to the repository format which would lead to infinite
            # recursion if we just called a_bzrdir.create_repository.
            a_bzrdir._ensure_real()
            result = a_bzrdir._real_bzrdir.create_repository(shared=shared)
        if not isinstance(result, RemoteRepository):
            return self.open(a_bzrdir)
        else:
            return result

    def initialize(self, a_bzrdir, shared=False):
        # Being asked to create on a non RemoteBzrDir:
        if not isinstance(a_bzrdir, RemoteBzrDir):
            return self._vfs_initialize(a_bzrdir, shared)
        medium = a_bzrdir._client._medium
        if medium._is_remote_before((1, 13)):
            return self._vfs_initialize(a_bzrdir, shared)
        # Creating on a remote bzr dir.
        # 1) get the network name to use.
        if self._custom_format:
            network_name = self._custom_format.network_name()
        else:
            # Select the current bzrlib default and ask for that.
            reference_bzrdir_format = bzrdir.format_registry.get('default')()
            reference_format = reference_bzrdir_format.repository_format
            network_name = reference_format.network_name()
        # 2) try direct creation via RPC
        path = a_bzrdir._path_for_remote_call(a_bzrdir._client)
        verb = 'BzrDir.create_repository'
        if shared:
            shared_str = 'True'
        else:
            shared_str = 'False'
        try:
            response = a_bzrdir._call(verb, path, network_name, shared_str)
        except errors.UnknownSmartMethod:
            # Fallback - use vfs methods
            return self._vfs_initialize(a_bzrdir, shared)
        else:
            # Turn the response into a RemoteRepository object.
            format = response_tuple_to_repo_format(response[1:])
            # Used to support creating a real format instance when needed.
            format._creating_bzrdir = a_bzrdir
            remote_repo = RemoteRepository(a_bzrdir, format)
            format._creating_repo = remote_repo
            return remote_repo

    def open(self, a_bzrdir):
        if not isinstance(a_bzrdir, RemoteBzrDir):
            raise AssertionError('%r is not a RemoteBzrDir' % (a_bzrdir,))
        return a_bzrdir.open_repository()

    def _ensure_real(self):
        if self._custom_format is None:
            self._custom_format = repository.network_format_registry.get(
                self._network_name)

    @property
    def _fetch_order(self):
        self._ensure_real()
        return self._custom_format._fetch_order

    @property
    def _fetch_uses_deltas(self):
        self._ensure_real()
        return self._custom_format._fetch_uses_deltas

    @property
    def _fetch_reconcile(self):
        self._ensure_real()
        return self._custom_format._fetch_reconcile

    def get_format_description(self):
        return 'bzr remote repository'

    def __eq__(self, other):
        return self.__class__ == other.__class__

    def check_conversion_target(self, target_format):
        if self.rich_root_data and not target_format.rich_root_data:
            raise errors.BadConversionTarget(
                'Does not support rich root data.', target_format)
        if (self.supports_tree_reference and
            not getattr(target_format, 'supports_tree_reference', False)):
            raise errors.BadConversionTarget(
                'Does not support nested trees', target_format)

    def network_name(self):
        if self._network_name:
            return self._network_name
        self._creating_repo._ensure_real()
        return self._creating_repo._real_repository._format.network_name()

    @property
    def _serializer(self):
        self._ensure_real()
        return self._custom_format._serializer


class RemoteRepository(_RpcHelper):
    """Repository accessed over rpc.

    For the moment most operations are performed using local transport-backed
    Repository objects.
    """

    def __init__(self, remote_bzrdir, format, real_repository=None, _client=None):
        """Create a RemoteRepository instance.

        :param remote_bzrdir: The bzrdir hosting this repository.
        :param format: The RemoteFormat object to use.
        :param real_repository: If not None, a local implementation of the
            repository logic for the repository, usually accessing the data
            via the VFS.
        :param _client: Private testing parameter - override the smart client
            to be used by the repository.
        """
        if real_repository:
            self._real_repository = real_repository
        else:
            self._real_repository = None
        self.bzrdir = remote_bzrdir
        if _client is None:
            self._client = remote_bzrdir._client
        else:
            self._client = _client
        self._format = format
        self._lock_mode = None
        self._lock_token = None
        self._lock_count = 0
        self._leave_lock = False
        self._unstacked_provider = graph.CachingParentsProvider(
            get_parent_map=self._get_parent_map_rpc)
        self._unstacked_provider.disable_cache()
        # For tests:
        # These depend on the actual remote format, so force them off for
        # maximum compatibility. XXX: In future these should depend on the
        # remote repository instance, but this is irrelevant until we perform
        # reconcile via an RPC call.
        self._reconcile_does_inventory_gc = False
        self._reconcile_fixes_text_parents = False
        self._reconcile_backsup_inventory = False
        self.base = self.bzrdir.transport.base
        # Additional places to query for data.
        self._fallback_repositories = []

    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, self.base)

    __repr__ = __str__

    def abort_write_group(self, suppress_errors=False):
        """Complete a write group on the decorated repository.

        Smart methods peform operations in a single step so this api
        is not really applicable except as a compatibility thunk
        for older plugins that don't use e.g. the CommitBuilder
        facility.

        :param suppress_errors: see Repository.abort_write_group.
        """
        self._ensure_real()
        return self._real_repository.abort_write_group(
            suppress_errors=suppress_errors)

    def commit_write_group(self):
        """Complete a write group on the decorated repository.

        Smart methods peform operations in a single step so this api
        is not really applicable except as a compatibility thunk
        for older plugins that don't use e.g. the CommitBuilder
        facility.
        """
        self._ensure_real()
        return self._real_repository.commit_write_group()

    def resume_write_group(self, tokens):
        self._ensure_real()
        return self._real_repository.resume_write_group(tokens)

    def suspend_write_group(self):
        self._ensure_real()
        return self._real_repository.suspend_write_group()

    def _ensure_real(self):
        """Ensure that there is a _real_repository set.

        Used before calls to self._real_repository.
        """
        if self._real_repository is None:
            self.bzrdir._ensure_real()
            self._set_real_repository(
                self.bzrdir._real_bzrdir.open_repository())

    def _translate_error(self, err, **context):
        self.bzrdir._translate_error(err, repository=self, **context)

    def find_text_key_references(self):
        """Find the text key references within the repository.

        :return: a dictionary mapping (file_id, revision_id) tuples to altered file-ids to an iterable of
        revision_ids. Each altered file-ids has the exact revision_ids that
        altered it listed explicitly.
        :return: A dictionary mapping text keys ((fileid, revision_id) tuples)
            to whether they were referred to by the inventory of the
            revision_id that they contain. The inventory texts from all present
            revision ids are assessed to generate this report.
        """
        self._ensure_real()
        return self._real_repository.find_text_key_references()

    def _generate_text_key_index(self):
        """Generate a new text key index for the repository.

        This is an expensive function that will take considerable time to run.

        :return: A dict mapping (file_id, revision_id) tuples to a list of
            parents, also (file_id, revision_id) tuples.
        """
        self._ensure_real()
        return self._real_repository._generate_text_key_index()

    @symbol_versioning.deprecated_method(symbol_versioning.one_four)
    def get_revision_graph(self, revision_id=None):
        """See Repository.get_revision_graph()."""
        return self._get_revision_graph(revision_id)

    def _get_revision_graph(self, revision_id):
        """Private method for using with old (< 1.2) servers to fallback."""
        if revision_id is None:
            revision_id = ''
        elif revision.is_null(revision_id):
            return {}

        path = self.bzrdir._path_for_remote_call(self._client)
        response = self._call_expecting_body(
            'Repository.get_revision_graph', path, revision_id)
        response_tuple, response_handler = response
        if response_tuple[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response_tuple)
        coded = response_handler.read_body_bytes()
        if coded == '':
            # no revisions in this repository!
            return {}
        lines = coded.split('\n')
        revision_graph = {}
        for line in lines:
            d = tuple(line.split())
            revision_graph[d[0]] = d[1:]

        return revision_graph

    def _get_sink(self):
        """See Repository._get_sink()."""
        return RemoteStreamSink(self)

    def _get_source(self, to_format):
        """Return a source for streaming from this repository."""
        return RemoteStreamSource(self, to_format)

    def has_revision(self, revision_id):
        """See Repository.has_revision()."""
        if revision_id == NULL_REVISION:
            # The null revision is always present.
            return True
        path = self.bzrdir._path_for_remote_call(self._client)
        response = self._call('Repository.has_revision', path, revision_id)
        if response[0] not in ('yes', 'no'):
            raise errors.UnexpectedSmartServerResponse(response)
        if response[0] == 'yes':
            return True
        for fallback_repo in self._fallback_repositories:
            if fallback_repo.has_revision(revision_id):
                return True
        return False

    def has_revisions(self, revision_ids):
        """See Repository.has_revisions()."""
        # FIXME: This does many roundtrips, particularly when there are
        # fallback repositories.  -- mbp 20080905
        result = set()
        for revision_id in revision_ids:
            if self.has_revision(revision_id):
                result.add(revision_id)
        return result

    def has_same_location(self, other):
        return (self.__class__ == other.__class__ and
                self.bzrdir.transport.base == other.bzrdir.transport.base)

    def get_graph(self, other_repository=None):
        """Return the graph for this repository format"""
        parents_provider = self._make_parents_provider(other_repository)
        return graph.Graph(parents_provider)

    def gather_stats(self, revid=None, committers=None):
        """See Repository.gather_stats()."""
        path = self.bzrdir._path_for_remote_call(self._client)
        # revid can be None to indicate no revisions, not just NULL_REVISION
        if revid is None or revision.is_null(revid):
            fmt_revid = ''
        else:
            fmt_revid = revid
        if committers is None or not committers:
            fmt_committers = 'no'
        else:
            fmt_committers = 'yes'
        response_tuple, response_handler = self._call_expecting_body(
            'Repository.gather_stats', path, fmt_revid, fmt_committers)
        if response_tuple[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response_tuple)

        body = response_handler.read_body_bytes()
        result = {}
        for line in body.split('\n'):
            if not line:
                continue
            key, val_text = line.split(':')
            if key in ('revisions', 'size', 'committers'):
                result[key] = int(val_text)
            elif key in ('firstrev', 'latestrev'):
                values = val_text.split(' ')[1:]
                result[key] = (float(values[0]), long(values[1]))

        return result

    def find_branches(self, using=False):
        """See Repository.find_branches()."""
        # should be an API call to the server.
        self._ensure_real()
        return self._real_repository.find_branches(using=using)

    def get_physical_lock_status(self):
        """See Repository.get_physical_lock_status()."""
        # should be an API call to the server.
        self._ensure_real()
        return self._real_repository.get_physical_lock_status()

    def is_in_write_group(self):
        """Return True if there is an open write group.

        write groups are only applicable locally for the smart server..
        """
        if self._real_repository:
            return self._real_repository.is_in_write_group()

    def is_locked(self):
        return self._lock_count >= 1

    def is_shared(self):
        """See Repository.is_shared()."""
        path = self.bzrdir._path_for_remote_call(self._client)
        response = self._call('Repository.is_shared', path)
        if response[0] not in ('yes', 'no'):
            raise SmartProtocolError('unexpected response code %s' % (response,))
        return response[0] == 'yes'

    def is_write_locked(self):
        return self._lock_mode == 'w'

    def lock_read(self):
        # wrong eventually - want a local lock cache context
        if not self._lock_mode:
            self._lock_mode = 'r'
            self._lock_count = 1
            self._unstacked_provider.enable_cache(cache_misses=False)
            if self._real_repository is not None:
                self._real_repository.lock_read()
        else:
            self._lock_count += 1

    def _remote_lock_write(self, token):
        path = self.bzrdir._path_for_remote_call(self._client)
        if token is None:
            token = ''
        err_context = {'token': token}
        response = self._call('Repository.lock_write', path, token,
                              **err_context)
        if response[0] == 'ok':
            ok, token = response
            return token
        else:
            raise errors.UnexpectedSmartServerResponse(response)

    def lock_write(self, token=None, _skip_rpc=False):
        if not self._lock_mode:
            if _skip_rpc:
                if self._lock_token is not None:
                    if token != self._lock_token:
                        raise errors.TokenMismatch(token, self._lock_token)
                self._lock_token = token
            else:
                self._lock_token = self._remote_lock_write(token)
            # if self._lock_token is None, then this is something like packs or
            # svn where we don't get to lock the repo, or a weave style repository
            # where we cannot lock it over the wire and attempts to do so will
            # fail.
            if self._real_repository is not None:
                self._real_repository.lock_write(token=self._lock_token)
            if token is not None:
                self._leave_lock = True
            else:
                self._leave_lock = False
            self._lock_mode = 'w'
            self._lock_count = 1
            self._unstacked_provider.enable_cache(cache_misses=False)
        elif self._lock_mode == 'r':
            raise errors.ReadOnlyError(self)
        else:
            self._lock_count += 1
        return self._lock_token or None

    def leave_lock_in_place(self):
        if not self._lock_token:
            raise NotImplementedError(self.leave_lock_in_place)
        self._leave_lock = True

    def dont_leave_lock_in_place(self):
        if not self._lock_token:
            raise NotImplementedError(self.dont_leave_lock_in_place)
        self._leave_lock = False

    def _set_real_repository(self, repository):
        """Set the _real_repository for this repository.

        :param repository: The repository to fallback to for non-hpss
            implemented operations.
        """
        if self._real_repository is not None:
            # Replacing an already set real repository.
            # We cannot do this [currently] if the repository is locked -
            # synchronised state might be lost.
            if self.is_locked():
                raise AssertionError('_real_repository is already set')
        if isinstance(repository, RemoteRepository):
            raise AssertionError()
        self._real_repository = repository
        for fb in self._fallback_repositories:
            self._real_repository.add_fallback_repository(fb)
        if self._lock_mode == 'w':
            # if we are already locked, the real repository must be able to
            # acquire the lock with our token.
            self._real_repository.lock_write(self._lock_token)
        elif self._lock_mode == 'r':
            self._real_repository.lock_read()

    def start_write_group(self):
        """Start a write group on the decorated repository.

        Smart methods peform operations in a single step so this api
        is not really applicable except as a compatibility thunk
        for older plugins that don't use e.g. the CommitBuilder
        facility.
        """
        self._ensure_real()
        return self._real_repository.start_write_group()

    def _unlock(self, token):
        path = self.bzrdir._path_for_remote_call(self._client)
        if not token:
            # with no token the remote repository is not persistently locked.
            return
        err_context = {'token': token}
        response = self._call('Repository.unlock', path, token,
                              **err_context)
        if response == ('ok',):
            return
        else:
            raise errors.UnexpectedSmartServerResponse(response)

    def unlock(self):
        if not self._lock_count:
            raise errors.LockNotHeld(self)
        self._lock_count -= 1
        if self._lock_count > 0:
            return
        self._unstacked_provider.disable_cache()
        old_mode = self._lock_mode
        self._lock_mode = None
        try:
            # The real repository is responsible at present for raising an
            # exception if it's in an unfinished write group.  However, it
            # normally will *not* actually remove the lock from disk - that's
            # done by the server on receiving the Repository.unlock call.
            # This is just to let the _real_repository stay up to date.
            if self._real_repository is not None:
                self._real_repository.unlock()
        finally:
            # The rpc-level lock should be released even if there was a
            # problem releasing the vfs-based lock.
            if old_mode == 'w':
                # Only write-locked repositories need to make a remote method
                # call to perfom the unlock.
                old_token = self._lock_token
                self._lock_token = None
                if not self._leave_lock:
                    self._unlock(old_token)

    def break_lock(self):
        # should hand off to the network
        self._ensure_real()
        return self._real_repository.break_lock()

    def _get_tarball(self, compression):
        """Return a TemporaryFile containing a repository tarball.

        Returns None if the server does not support sending tarballs.
        """
        import tempfile
        path = self.bzrdir._path_for_remote_call(self._client)
        try:
            response, protocol = self._call_expecting_body(
                'Repository.tarball', path, compression)
        except errors.UnknownSmartMethod:
            protocol.cancel_read_body()
            return None
        if response[0] == 'ok':
            # Extract the tarball and return it
            t = tempfile.NamedTemporaryFile()
            # TODO: rpc layer should read directly into it...
            t.write(protocol.read_body_bytes())
            t.seek(0)
            return t
        raise errors.UnexpectedSmartServerResponse(response)

    def sprout(self, to_bzrdir, revision_id=None):
        # TODO: Option to control what format is created?
        self._ensure_real()
        dest_repo = self._real_repository._format.initialize(to_bzrdir,
                                                             shared=False)
        dest_repo.fetch(self, revision_id=revision_id)
        return dest_repo

    ### These methods are just thin shims to the VFS object for now.

    def revision_tree(self, revision_id):
        self._ensure_real()
        return self._real_repository.revision_tree(revision_id)

    def get_serializer_format(self):
        self._ensure_real()
        return self._real_repository.get_serializer_format()

    def get_commit_builder(self, branch, parents, config, timestamp=None,
                           timezone=None, committer=None, revprops=None,
                           revision_id=None):
        # FIXME: It ought to be possible to call this without immediately
        # triggering _ensure_real.  For now it's the easiest thing to do.
        self._ensure_real()
        real_repo = self._real_repository
        builder = real_repo.get_commit_builder(branch, parents,
                config, timestamp=timestamp, timezone=timezone,
                committer=committer, revprops=revprops, revision_id=revision_id)
        return builder

    def add_fallback_repository(self, repository):
        """Add a repository to use for looking up data not held locally.

        :param repository: A repository.
        """
        # XXX: At the moment the RemoteRepository will allow fallbacks
        # unconditionally - however, a _real_repository will usually exist,
        # and may raise an error if it's not accommodated by the underlying
        # format.  Eventually we should check when opening the repository
        # whether it's willing to allow them or not.
        #
        # We need to accumulate additional repositories here, to pass them in
        # on various RPC's.
        #
        self._fallback_repositories.append(repository)
        # If self._real_repository was parameterised already (e.g. because a
        # _real_branch had its get_stacked_on_url method called), then the
        # repository to be added may already be in the _real_repositories list.
        if self._real_repository is not None:
            if repository not in self._real_repository._fallback_repositories:
                self._real_repository.add_fallback_repository(repository)
        else:
            # They are also seen by the fallback repository.  If it doesn't
            # exist yet they'll be added then.  This implicitly copies them.
            self._ensure_real()

    def add_inventory(self, revid, inv, parents):
        self._ensure_real()
        return self._real_repository.add_inventory(revid, inv, parents)

    def add_inventory_by_delta(self, basis_revision_id, delta, new_revision_id,
                               parents):
        self._ensure_real()
        return self._real_repository.add_inventory_by_delta(basis_revision_id,
            delta, new_revision_id, parents)

    def add_revision(self, rev_id, rev, inv=None, config=None):
        self._ensure_real()
        return self._real_repository.add_revision(
            rev_id, rev, inv=inv, config=config)

    @needs_read_lock
    def get_inventory(self, revision_id):
        self._ensure_real()
        return self._real_repository.get_inventory(revision_id)

    def iter_inventories(self, revision_ids):
        self._ensure_real()
        return self._real_repository.iter_inventories(revision_ids)

    @needs_read_lock
    def get_revision(self, revision_id):
        self._ensure_real()
        return self._real_repository.get_revision(revision_id)

    def get_transaction(self):
        self._ensure_real()
        return self._real_repository.get_transaction()

    @needs_read_lock
    def clone(self, a_bzrdir, revision_id=None):
        self._ensure_real()
        return self._real_repository.clone(a_bzrdir, revision_id=revision_id)

    def make_working_trees(self):
        """See Repository.make_working_trees"""
        self._ensure_real()
        return self._real_repository.make_working_trees()

    def revision_ids_to_search_result(self, result_set):
        """Convert a set of revision ids to a graph SearchResult."""
        result_parents = set()
        for parents in self.get_graph().get_parent_map(
            result_set).itervalues():
            result_parents.update(parents)
        included_keys = result_set.intersection(result_parents)
        start_keys = result_set.difference(included_keys)
        exclude_keys = result_parents.difference(result_set)
        result = graph.SearchResult(start_keys, exclude_keys,
            len(result_set), result_set)
        return result

    @needs_read_lock
    def search_missing_revision_ids(self, other, revision_id=None, find_ghosts=True):
        """Return the revision ids that other has that this does not.

        These are returned in topological order.

        revision_id: only return revision ids included by revision_id.
        """
        return repository.InterRepository.get(
            other, self).search_missing_revision_ids(revision_id, find_ghosts)

    def fetch(self, source, revision_id=None, pb=None, find_ghosts=False,
            fetch_spec=None):
        if fetch_spec is not None and revision_id is not None:
            raise AssertionError(
                "fetch_spec and revision_id are mutually exclusive.")
        # Not delegated to _real_repository so that InterRepository.get has a
        # chance to find an InterRepository specialised for RemoteRepository.
        if self.has_same_location(source) and fetch_spec is None:
            # check that last_revision is in 'from' and then return a
            # no-operation.
            if (revision_id is not None and
                not revision.is_null(revision_id)):
                self.get_revision(revision_id)
            return 0, []
        inter = repository.InterRepository.get(source, self)
        try:
            return inter.fetch(revision_id=revision_id, pb=pb,
                    find_ghosts=find_ghosts, fetch_spec=fetch_spec)
        except NotImplementedError:
            raise errors.IncompatibleRepositories(source, self)

    def create_bundle(self, target, base, fileobj, format=None):
        self._ensure_real()
        self._real_repository.create_bundle(target, base, fileobj, format)

    @needs_read_lock
    def get_ancestry(self, revision_id, topo_sorted=True):
        self._ensure_real()
        return self._real_repository.get_ancestry(revision_id, topo_sorted)

    def fileids_altered_by_revision_ids(self, revision_ids):
        self._ensure_real()
        return self._real_repository.fileids_altered_by_revision_ids(revision_ids)

    def _get_versioned_file_checker(self, revisions, revision_versions_cache):
        self._ensure_real()
        return self._real_repository._get_versioned_file_checker(
            revisions, revision_versions_cache)

    def iter_files_bytes(self, desired_files):
        """See Repository.iter_file_bytes.
        """
        self._ensure_real()
        return self._real_repository.iter_files_bytes(desired_files)

    def get_parent_map(self, revision_ids):
        """See bzrlib.Graph.get_parent_map()."""
        return self._make_parents_provider().get_parent_map(revision_ids)

    def _get_parent_map_rpc(self, keys):
        """Helper for get_parent_map that performs the RPC."""
        medium = self._client._medium
        if medium._is_remote_before((1, 2)):
            # We already found out that the server can't understand
            # Repository.get_parent_map requests, so just fetch the whole
            # graph.
            # XXX: Note that this will issue a deprecation warning. This is ok
            # :- its because we're working with a deprecated server anyway, and
            # the user will almost certainly have seen a warning about the
            # server version already.
            rg = self.get_revision_graph()
            # There is an api discrepency between get_parent_map and
            # get_revision_graph. Specifically, a "key:()" pair in
            # get_revision_graph just means a node has no parents. For
            # "get_parent_map" it means the node is a ghost. So fix up the
            # graph to correct this.
            #   https://bugs.launchpad.net/bzr/+bug/214894
            # There is one other "bug" which is that ghosts in
            # get_revision_graph() are not returned at all. But we won't worry
            # about that for now.
            for node_id, parent_ids in rg.iteritems():
                if parent_ids == ():
                    rg[node_id] = (NULL_REVISION,)
            rg[NULL_REVISION] = ()
            return rg

        keys = set(keys)
        if None in keys:
            raise ValueError('get_parent_map(None) is not valid')
        if NULL_REVISION in keys:
            keys.discard(NULL_REVISION)
            found_parents = {NULL_REVISION:()}
            if not keys:
                return found_parents
        else:
            found_parents = {}
        # TODO(Needs analysis): We could assume that the keys being requested
        # from get_parent_map are in a breadth first search, so typically they
        # will all be depth N from some common parent, and we don't have to
        # have the server iterate from the root parent, but rather from the
        # keys we're searching; and just tell the server the keyspace we
        # already have; but this may be more traffic again.

        # Transform self._parents_map into a search request recipe.
        # TODO: Manage this incrementally to avoid covering the same path
        # repeatedly. (The server will have to on each request, but the less
        # work done the better).
        parents_map = self._unstacked_provider.get_cached_map()
        if parents_map is None:
            # Repository is not locked, so there's no cache.
            parents_map = {}
        start_set = set(parents_map)
        result_parents = set()
        for parents in parents_map.itervalues():
            result_parents.update(parents)
        stop_keys = result_parents.difference(start_set)
        included_keys = start_set.intersection(result_parents)
        start_set.difference_update(included_keys)
        recipe = (start_set, stop_keys, len(parents_map))
        body = self._serialise_search_recipe(recipe)
        path = self.bzrdir._path_for_remote_call(self._client)
        for key in keys:
            if type(key) is not str:
                raise ValueError(
                    "key %r not a plain string" % (key,))
        verb = 'Repository.get_parent_map'
        args = (path,) + tuple(keys)
        try:
            response = self._call_with_body_bytes_expecting_body(
                verb, args, body)
        except errors.UnknownSmartMethod:
            # Server does not support this method, so get the whole graph.
            # Worse, we have to force a disconnection, because the server now
            # doesn't realise it has a body on the wire to consume, so the
            # only way to recover is to abandon the connection.
            warning(
                'Server is too old for fast get_parent_map, reconnecting.  '
                '(Upgrade the server to Bazaar 1.2 to avoid this)')
            medium.disconnect()
            # To avoid having to disconnect repeatedly, we keep track of the
            # fact the server doesn't understand remote methods added in 1.2.
            medium._remember_remote_is_before((1, 2))
            return self.get_revision_graph(None)
        response_tuple, response_handler = response
        if response_tuple[0] not in ['ok']:
            response_handler.cancel_read_body()
            raise errors.UnexpectedSmartServerResponse(response_tuple)
        if response_tuple[0] == 'ok':
            coded = bz2.decompress(response_handler.read_body_bytes())
            if coded == '':
                # no revisions found
                return {}
            lines = coded.split('\n')
            revision_graph = {}
            for line in lines:
                d = tuple(line.split())
                if len(d) > 1:
                    revision_graph[d[0]] = d[1:]
                else:
                    # No parents - so give the Graph result (NULL_REVISION,).
                    revision_graph[d[0]] = (NULL_REVISION,)
            return revision_graph

    @needs_read_lock
    def get_signature_text(self, revision_id):
        self._ensure_real()
        return self._real_repository.get_signature_text(revision_id)

    @needs_read_lock
    @symbol_versioning.deprecated_method(symbol_versioning.one_three)
    def get_revision_graph_with_ghosts(self, revision_ids=None):
        self._ensure_real()
        return self._real_repository.get_revision_graph_with_ghosts(
            revision_ids=revision_ids)

    @needs_read_lock
    def get_inventory_xml(self, revision_id):
        self._ensure_real()
        return self._real_repository.get_inventory_xml(revision_id)

    def deserialise_inventory(self, revision_id, xml):
        self._ensure_real()
        return self._real_repository.deserialise_inventory(revision_id, xml)

    def reconcile(self, other=None, thorough=False):
        self._ensure_real()
        return self._real_repository.reconcile(other=other, thorough=thorough)

    def all_revision_ids(self):
        self._ensure_real()
        return self._real_repository.all_revision_ids()

    @needs_read_lock
    def get_deltas_for_revisions(self, revisions):
        self._ensure_real()
        return self._real_repository.get_deltas_for_revisions(revisions)

    @needs_read_lock
    def get_revision_delta(self, revision_id):
        self._ensure_real()
        return self._real_repository.get_revision_delta(revision_id)

    @needs_read_lock
    def revision_trees(self, revision_ids):
        self._ensure_real()
        return self._real_repository.revision_trees(revision_ids)

    @needs_read_lock
    def get_revision_reconcile(self, revision_id):
        self._ensure_real()
        return self._real_repository.get_revision_reconcile(revision_id)

    @needs_read_lock
    def check(self, revision_ids=None):
        self._ensure_real()
        return self._real_repository.check(revision_ids=revision_ids)

    def copy_content_into(self, destination, revision_id=None):
        self._ensure_real()
        return self._real_repository.copy_content_into(
            destination, revision_id=revision_id)

    def _copy_repository_tarball(self, to_bzrdir, revision_id=None):
        # get a tarball of the remote repository, and copy from that into the
        # destination
        from bzrlib import osutils
        import tarfile
        # TODO: Maybe a progress bar while streaming the tarball?
        note("Copying repository content as tarball...")
        tar_file = self._get_tarball('bz2')
        if tar_file is None:
            return None
        destination = to_bzrdir.create_repository()
        try:
            tar = tarfile.open('repository', fileobj=tar_file,
                mode='r|bz2')
            tmpdir = osutils.mkdtemp()
            try:
                _extract_tar(tar, tmpdir)
                tmp_bzrdir = BzrDir.open(tmpdir)
                tmp_repo = tmp_bzrdir.open_repository()
                tmp_repo.copy_content_into(destination, revision_id)
            finally:
                osutils.rmtree(tmpdir)
        finally:
            tar_file.close()
        return destination
        # TODO: Suggestion from john: using external tar is much faster than
        # python's tarfile library, but it may not work on windows.

    @property
    def inventories(self):
        """Decorate the real repository for now.

        In the long term a full blown network facility is needed to
        avoid creating a real repository object locally.
        """
        self._ensure_real()
        return self._real_repository.inventories

    @needs_write_lock
    def pack(self):
        """Compress the data within the repository.

        This is not currently implemented within the smart server.
        """
        self._ensure_real()
        return self._real_repository.pack()

    @property
    def revisions(self):
        """Decorate the real repository for now.

        In the short term this should become a real object to intercept graph
        lookups.

        In the long term a full blown network facility is needed.
        """
        self._ensure_real()
        return self._real_repository.revisions

    def set_make_working_trees(self, new_value):
        if new_value:
            new_value_str = "True"
        else:
            new_value_str = "False"
        path = self.bzrdir._path_for_remote_call(self._client)
        try:
            response = self._call(
                'Repository.set_make_working_trees', path, new_value_str)
        except errors.UnknownSmartMethod:
            self._ensure_real()
            self._real_repository.set_make_working_trees(new_value)
        else:
            if response[0] != 'ok':
                raise errors.UnexpectedSmartServerResponse(response)

    @property
    def signatures(self):
        """Decorate the real repository for now.

        In the long term a full blown network facility is needed to avoid
        creating a real repository object locally.
        """
        self._ensure_real()
        return self._real_repository.signatures

    @needs_write_lock
    def sign_revision(self, revision_id, gpg_strategy):
        self._ensure_real()
        return self._real_repository.sign_revision(revision_id, gpg_strategy)

    @property
    def texts(self):
        """Decorate the real repository for now.

        In the long term a full blown network facility is needed to avoid
        creating a real repository object locally.
        """
        self._ensure_real()
        return self._real_repository.texts

    @needs_read_lock
    def get_revisions(self, revision_ids):
        self._ensure_real()
        return self._real_repository.get_revisions(revision_ids)

    def supports_rich_root(self):
        return self._format.rich_root_data

    def iter_reverse_revision_history(self, revision_id):
        self._ensure_real()
        return self._real_repository.iter_reverse_revision_history(revision_id)

    @property
    def _serializer(self):
        return self._format._serializer

    def store_revision_signature(self, gpg_strategy, plaintext, revision_id):
        self._ensure_real()
        return self._real_repository.store_revision_signature(
            gpg_strategy, plaintext, revision_id)

    def add_signature_text(self, revision_id, signature):
        self._ensure_real()
        return self._real_repository.add_signature_text(revision_id, signature)

    def has_signature_for_revision_id(self, revision_id):
        self._ensure_real()
        return self._real_repository.has_signature_for_revision_id(revision_id)

    def item_keys_introduced_by(self, revision_ids, _files_pb=None):
        self._ensure_real()
        return self._real_repository.item_keys_introduced_by(revision_ids,
            _files_pb=_files_pb)

    def revision_graph_can_have_wrong_parents(self):
        # The answer depends on the remote repo format.
        self._ensure_real()
        return self._real_repository.revision_graph_can_have_wrong_parents()

    def _find_inconsistent_revision_parents(self):
        self._ensure_real()
        return self._real_repository._find_inconsistent_revision_parents()

    def _check_for_inconsistent_revision_parents(self):
        self._ensure_real()
        return self._real_repository._check_for_inconsistent_revision_parents()

    def _make_parents_provider(self, other=None):
        providers = [self._unstacked_provider]
        if other is not None:
            providers.insert(0, other)
        providers.extend(r._make_parents_provider() for r in
                         self._fallback_repositories)
        return graph._StackedParentsProvider(providers)

    def _serialise_search_recipe(self, recipe):
        """Serialise a graph search recipe.

        :param recipe: A search recipe (start, stop, count).
        :return: Serialised bytes.
        """
        start_keys = ' '.join(recipe[0])
        stop_keys = ' '.join(recipe[1])
        count = str(recipe[2])
        return '\n'.join((start_keys, stop_keys, count))

    def _serialise_search_result(self, search_result):
        if isinstance(search_result, graph.PendingAncestryResult):
            parts = ['ancestry-of']
            parts.extend(search_result.heads)
        else:
            recipe = search_result.get_recipe()
            parts = ['search', self._serialise_search_recipe(recipe)]
        return '\n'.join(parts)

    def autopack(self):
        path = self.bzrdir._path_for_remote_call(self._client)
        try:
            response = self._call('PackRepository.autopack', path)
        except errors.UnknownSmartMethod:
            self._ensure_real()
            self._real_repository._pack_collection.autopack()
            return
        if self._real_repository is not None:
            # Reset the real repository's cache of pack names.
            # XXX: At some point we may be able to skip this and just rely on
            # the automatic retry logic to do the right thing, but for now we
            # err on the side of being correct rather than being optimal.
            self._real_repository._pack_collection.reload_pack_names()
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)


class RemoteStreamSink(repository.StreamSink):

    def _insert_real(self, stream, src_format, resume_tokens):
        self.target_repo._ensure_real()
        sink = self.target_repo._real_repository._get_sink()
        result = sink.insert_stream(stream, src_format, resume_tokens)
        if not result:
            self.target_repo.autopack()
        return result

    def insert_stream(self, stream, src_format, resume_tokens):
        repo = self.target_repo
        client = repo._client
        medium = client._medium
        if medium._is_remote_before((1, 13)):
            # No possible way this can work.
            return self._insert_real(stream, src_format, resume_tokens)
        path = repo.bzrdir._path_for_remote_call(client)
        if not resume_tokens:
            # XXX: Ugly but important for correctness, *will* be fixed during
            # 1.13 cycle. Pushing a stream that is interrupted results in a
            # fallback to the _real_repositories sink *with a partial stream*.
            # Thats bad because we insert less data than bzr expected. To avoid
            # this we do a trial push to make sure the verb is accessible, and
            # do not fallback when actually pushing the stream. A cleanup patch
            # is going to look at rewinding/restarting the stream/partial
            # buffering etc.
            byte_stream = smart_repo._stream_to_byte_stream([], src_format)
            try:
                response = client.call_with_body_stream(
                    ('Repository.insert_stream', path, ''), byte_stream)
            except errors.UnknownSmartMethod:
                medium._remember_remote_is_before((1,13))
                return self._insert_real(stream, src_format, resume_tokens)
        byte_stream = smart_repo._stream_to_byte_stream(
            stream, src_format)
        resume_tokens = ' '.join(resume_tokens)
        response = client.call_with_body_stream(
            ('Repository.insert_stream', path, resume_tokens), byte_stream)
        if response[0][0] not in ('ok', 'missing-basis'):
            raise errors.UnexpectedSmartServerResponse(response)
        if response[0][0] == 'missing-basis':
            tokens, missing_keys = bencode.bdecode_as_tuple(response[0][1])
            resume_tokens = tokens
            return resume_tokens, missing_keys
        else:
            if self.target_repo._real_repository is not None:
                collection = getattr(self.target_repo._real_repository,
                    '_pack_collection', None)
                if collection is not None:
                    collection.reload_pack_names()
            return [], set()


class RemoteStreamSource(repository.StreamSource):
    """Stream data from a remote server."""

    def get_stream(self, search):
        # streaming with fallback repositories is not well defined yet: The
        # remote repository cannot see the fallback repositories, and thus
        # cannot satisfy the entire search in the general case. Likewise the
        # fallback repositories cannot reify the search to determine what they
        # should send. It likely needs a return value in the stream listing the
        # edge of the search to resume from in fallback repositories.
        if self.from_repository._fallback_repositories:
            return repository.StreamSource.get_stream(self, search)
        repo = self.from_repository
        client = repo._client
        medium = client._medium
        if medium._is_remote_before((1, 13)):
            # No possible way this can work.
            return repository.StreamSource.get_stream(self, search)
        path = repo.bzrdir._path_for_remote_call(client)
        try:
            search_bytes = repo._serialise_search_result(search)
            response = repo._call_with_body_bytes_expecting_body(
                'Repository.get_stream',
                (path, self.to_format.network_name()), search_bytes)
            response_tuple, response_handler = response
        except errors.UnknownSmartMethod:
            medium._remember_remote_is_before((1,13))
            return repository.StreamSource.get_stream(self, search)
        if response_tuple[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response_tuple)
        byte_stream = response_handler.read_streamed_body()
        src_format, stream = smart_repo._byte_stream_to_stream(byte_stream)
        if src_format.network_name() != repo._format.network_name():
            raise AssertionError(
                "Mismatched RemoteRepository and stream src %r, %r" % (
                src_format.network_name(), repo._format.network_name()))
        return stream


class RemoteBranchLockableFiles(LockableFiles):
    """A 'LockableFiles' implementation that talks to a smart server.

    This is not a public interface class.
    """

    def __init__(self, bzrdir, _client):
        self.bzrdir = bzrdir
        self._client = _client
        self._need_find_modes = True
        LockableFiles.__init__(
            self, bzrdir.get_branch_transport(None),
            'lock', lockdir.LockDir)

    def _find_modes(self):
        # RemoteBranches don't let the client set the mode of control files.
        self._dir_mode = None
        self._file_mode = None


class RemoteBranchFormat(branch.BranchFormat):

    def __init__(self):
        super(RemoteBranchFormat, self).__init__()
        self._matchingbzrdir = RemoteBzrDirFormat()
        self._matchingbzrdir.set_branch_format(self)
        self._custom_format = None

    def __eq__(self, other):
        return (isinstance(other, RemoteBranchFormat) and
            self.__dict__ == other.__dict__)

    def get_format_description(self):
        return 'Remote BZR Branch'

    def network_name(self):
        return self._network_name

    def open(self, a_bzrdir):
        return a_bzrdir.open_branch()

    def _vfs_initialize(self, a_bzrdir):
        # Initialisation when using a local bzrdir object, or a non-vfs init
        # method is not available on the server.
        # self._custom_format is always set - the start of initialize ensures
        # that.
        if isinstance(a_bzrdir, RemoteBzrDir):
            a_bzrdir._ensure_real()
            result = self._custom_format.initialize(a_bzrdir._real_bzrdir)
        else:
            # We assume the bzrdir is parameterised; it may not be.
            result = self._custom_format.initialize(a_bzrdir)
        if (isinstance(a_bzrdir, RemoteBzrDir) and
            not isinstance(result, RemoteBranch)):
            result = RemoteBranch(a_bzrdir, a_bzrdir.find_repository(), result)
        return result

    def initialize(self, a_bzrdir):
        # 1) get the network name to use.
        if self._custom_format:
            network_name = self._custom_format.network_name()
        else:
            # Select the current bzrlib default and ask for that.
            reference_bzrdir_format = bzrdir.format_registry.get('default')()
            reference_format = reference_bzrdir_format.get_branch_format()
            self._custom_format = reference_format
            network_name = reference_format.network_name()
        # Being asked to create on a non RemoteBzrDir:
        if not isinstance(a_bzrdir, RemoteBzrDir):
            return self._vfs_initialize(a_bzrdir)
        medium = a_bzrdir._client._medium
        if medium._is_remote_before((1, 13)):
            return self._vfs_initialize(a_bzrdir)
        # Creating on a remote bzr dir.
        # 2) try direct creation via RPC
        path = a_bzrdir._path_for_remote_call(a_bzrdir._client)
        verb = 'BzrDir.create_branch'
        try:
            response = a_bzrdir._call(verb, path, network_name)
        except errors.UnknownSmartMethod:
            # Fallback - use vfs methods
            return self._vfs_initialize(a_bzrdir)
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        # Turn the response into a RemoteRepository object.
        format = RemoteBranchFormat()
        format._network_name = response[1]
        repo_format = response_tuple_to_repo_format(response[3:])
        if response[2] == '':
            repo_bzrdir = a_bzrdir
        else:
            repo_bzrdir = RemoteBzrDir(
                a_bzrdir.root_transport.clone(response[2]), a_bzrdir._format,
                a_bzrdir._client)
        remote_repo = RemoteRepository(repo_bzrdir, repo_format)
        remote_branch = RemoteBranch(a_bzrdir, remote_repo,
            format=format, setup_stacking=False)
        # XXX: We know this is a new branch, so it must have revno 0, revid
        # NULL_REVISION. Creating the branch locked would make this be unable
        # to be wrong; here its simply very unlikely to be wrong. RBC 20090225
        remote_branch._last_revision_info_cache = 0, NULL_REVISION
        return remote_branch

    def supports_tags(self):
        # Remote branches might support tags, but we won't know until we
        # access the real remote branch.
        return True


class RemoteBranch(branch.Branch, _RpcHelper):
    """Branch stored on a server accessed by HPSS RPC.

    At the moment most operations are mapped down to simple file operations.
    """

    def __init__(self, remote_bzrdir, remote_repository, real_branch=None,
        _client=None, format=None, setup_stacking=True):
        """Create a RemoteBranch instance.

        :param real_branch: An optional local implementation of the branch
            format, usually accessing the data via the VFS.
        :param _client: Private parameter for testing.
        :param format: A RemoteBranchFormat object, None to create one
            automatically. If supplied it should have a network_name already
            supplied.
        :param setup_stacking: If True make an RPC call to determine the
            stacked (or not) status of the branch. If False assume the branch
            is not stacked.
        """
        # We intentionally don't call the parent class's __init__, because it
        # will try to assign to self.tags, which is a property in this subclass.
        # And the parent's __init__ doesn't do much anyway.
        self._revision_id_to_revno_cache = None
        self._partial_revision_id_to_revno_cache = {}
        self._revision_history_cache = None
        self._last_revision_info_cache = None
        self._merge_sorted_revisions_cache = None
        self.bzrdir = remote_bzrdir
        if _client is not None:
            self._client = _client
        else:
            self._client = remote_bzrdir._client
        self.repository = remote_repository
        if real_branch is not None:
            self._real_branch = real_branch
            # Give the remote repository the matching real repo.
            real_repo = self._real_branch.repository
            if isinstance(real_repo, RemoteRepository):
                real_repo._ensure_real()
                real_repo = real_repo._real_repository
            self.repository._set_real_repository(real_repo)
            # Give the branch the remote repository to let fast-pathing happen.
            self._real_branch.repository = self.repository
        else:
            self._real_branch = None
        # Fill out expected attributes of branch for bzrlib api users.
        self.base = self.bzrdir.root_transport.base
        self._control_files = None
        self._lock_mode = None
        self._lock_token = None
        self._repo_lock_token = None
        self._lock_count = 0
        self._leave_lock = False
        # Setup a format: note that we cannot call _ensure_real until all the
        # attributes above are set: This code cannot be moved higher up in this
        # function.
        if format is None:
            self._format = RemoteBranchFormat()
            if real_branch is not None:
                self._format._network_name = \
                    self._real_branch._format.network_name()
            #else:
            #    # XXX: Need to get this from BzrDir.open_branch's return value.
            #    self._ensure_real()
            #    self._format._network_name = \
            #        self._real_branch._format.network_name()
        else:
            self._format = format
        # The base class init is not called, so we duplicate this:
        hooks = branch.Branch.hooks['open']
        for hook in hooks:
            hook(self)
        if setup_stacking:
            self._setup_stacking()

    def _setup_stacking(self):
        # configure stacking into the remote repository, by reading it from
        # the vfs branch.
        try:
            fallback_url = self.get_stacked_on_url()
        except (errors.NotStacked, errors.UnstackableBranchFormat,
            errors.UnstackableRepositoryFormat), e:
            return
        # it's relative to this branch...
        fallback_url = urlutils.join(self.base, fallback_url)
        transports = [self.bzrdir.root_transport]
        if self._real_branch is not None:
            # The real repository is setup already:
            transports.append(self._real_branch._transport)
            self.repository.add_fallback_repository(
                self.repository._real_repository._fallback_repositories[0])
        else:
            stacked_on = branch.Branch.open(fallback_url,
                                            possible_transports=transports)
            self.repository.add_fallback_repository(stacked_on.repository)

    def _get_real_transport(self):
        # if we try vfs access, return the real branch's vfs transport
        self._ensure_real()
        return self._real_branch._transport

    _transport = property(_get_real_transport)

    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, self.base)

    __repr__ = __str__

    def _ensure_real(self):
        """Ensure that there is a _real_branch set.

        Used before calls to self._real_branch.
        """
        if self._real_branch is None:
            if not vfs.vfs_enabled():
                raise AssertionError('smart server vfs must be enabled '
                    'to use vfs implementation')
            self.bzrdir._ensure_real()
            self._real_branch = self.bzrdir._real_bzrdir.open_branch()
            if self.repository._real_repository is None:
                # Give the remote repository the matching real repo.
                real_repo = self._real_branch.repository
                if isinstance(real_repo, RemoteRepository):
                    real_repo._ensure_real()
                    real_repo = real_repo._real_repository
                self.repository._set_real_repository(real_repo)
            # Give the real branch the remote repository to let fast-pathing
            # happen.
            self._real_branch.repository = self.repository
            if self._lock_mode == 'r':
                self._real_branch.lock_read()
            elif self._lock_mode == 'w':
                self._real_branch.lock_write(token=self._lock_token)

    def _translate_error(self, err, **context):
        self.repository._translate_error(err, branch=self, **context)

    def _clear_cached_state(self):
        super(RemoteBranch, self)._clear_cached_state()
        if self._real_branch is not None:
            self._real_branch._clear_cached_state()

    def _clear_cached_state_of_remote_branch_only(self):
        """Like _clear_cached_state, but doesn't clear the cache of
        self._real_branch.

        This is useful when falling back to calling a method of
        self._real_branch that changes state.  In that case the underlying
        branch changes, so we need to invalidate this RemoteBranch's cache of
        it.  However, there's no need to invalidate the _real_branch's cache
        too, in fact doing so might harm performance.
        """
        super(RemoteBranch, self)._clear_cached_state()

    @property
    def control_files(self):
        # Defer actually creating RemoteBranchLockableFiles until its needed,
        # because it triggers an _ensure_real that we otherwise might not need.
        if self._control_files is None:
            self._control_files = RemoteBranchLockableFiles(
                self.bzrdir, self._client)
        return self._control_files

    def _get_checkout_format(self):
        self._ensure_real()
        return self._real_branch._get_checkout_format()

    def get_physical_lock_status(self):
        """See Branch.get_physical_lock_status()."""
        # should be an API call to the server, as branches must be lockable.
        self._ensure_real()
        return self._real_branch.get_physical_lock_status()

    def get_stacked_on_url(self):
        """Get the URL this branch is stacked against.

        :raises NotStacked: If the branch is not stacked.
        :raises UnstackableBranchFormat: If the branch does not support
            stacking.
        :raises UnstackableRepositoryFormat: If the repository does not support
            stacking.
        """
        try:
            # there may not be a repository yet, so we can't use
            # self._translate_error, so we can't use self._call either.
            response = self._client.call('Branch.get_stacked_on_url',
                self._remote_path())
        except errors.ErrorFromSmartServer, err:
            # there may not be a repository yet, so we can't call through
            # its _translate_error
            _translate_error(err, branch=self)
        except errors.UnknownSmartMethod, err:
            self._ensure_real()
            return self._real_branch.get_stacked_on_url()
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        return response[1]

    def lock_read(self):
        self.repository.lock_read()
        if not self._lock_mode:
            self._lock_mode = 'r'
            self._lock_count = 1
            if self._real_branch is not None:
                self._real_branch.lock_read()
        else:
            self._lock_count += 1

    def _remote_lock_write(self, token):
        if token is None:
            branch_token = repo_token = ''
        else:
            branch_token = token
            repo_token = self.repository.lock_write()
            self.repository.unlock()
        err_context = {'token': token}
        response = self._call(
            'Branch.lock_write', self._remote_path(), branch_token,
            repo_token or '', **err_context)
        if response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        ok, branch_token, repo_token = response
        return branch_token, repo_token

    def lock_write(self, token=None):
        if not self._lock_mode:
            # Lock the branch and repo in one remote call.
            remote_tokens = self._remote_lock_write(token)
            self._lock_token, self._repo_lock_token = remote_tokens
            if not self._lock_token:
                raise SmartProtocolError('Remote server did not return a token!')
            # Tell the self.repository object that it is locked.
            self.repository.lock_write(
                self._repo_lock_token, _skip_rpc=True)

            if self._real_branch is not None:
                self._real_branch.lock_write(token=self._lock_token)
            if token is not None:
                self._leave_lock = True
            else:
                self._leave_lock = False
            self._lock_mode = 'w'
            self._lock_count = 1
        elif self._lock_mode == 'r':
            raise errors.ReadOnlyTransaction
        else:
            if token is not None:
                # A token was given to lock_write, and we're relocking, so
                # check that the given token actually matches the one we
                # already have.
                if token != self._lock_token:
                    raise errors.TokenMismatch(token, self._lock_token)
            self._lock_count += 1
            # Re-lock the repository too.
            self.repository.lock_write(self._repo_lock_token)
        return self._lock_token or None

    def _unlock(self, branch_token, repo_token):
        err_context = {'token': str((branch_token, repo_token))}
        response = self._call(
            'Branch.unlock', self._remote_path(), branch_token,
            repo_token or '', **err_context)
        if response == ('ok',):
            return
        raise errors.UnexpectedSmartServerResponse(response)

    def unlock(self):
        try:
            self._lock_count -= 1
            if not self._lock_count:
                self._clear_cached_state()
                mode = self._lock_mode
                self._lock_mode = None
                if self._real_branch is not None:
                    if (not self._leave_lock and mode == 'w' and
                        self._repo_lock_token):
                        # If this RemoteBranch will remove the physical lock
                        # for the repository, make sure the _real_branch
                        # doesn't do it first.  (Because the _real_branch's
                        # repository is set to be the RemoteRepository.)
                        self._real_branch.repository.leave_lock_in_place()
                    self._real_branch.unlock()
                if mode != 'w':
                    # Only write-locked branched need to make a remote method
                    # call to perfom the unlock.
                    return
                if not self._lock_token:
                    raise AssertionError('Locked, but no token!')
                branch_token = self._lock_token
                repo_token = self._repo_lock_token
                self._lock_token = None
                self._repo_lock_token = None
                if not self._leave_lock:
                    self._unlock(branch_token, repo_token)
        finally:
            self.repository.unlock()

    def break_lock(self):
        self._ensure_real()
        return self._real_branch.break_lock()

    def leave_lock_in_place(self):
        if not self._lock_token:
            raise NotImplementedError(self.leave_lock_in_place)
        self._leave_lock = True

    def dont_leave_lock_in_place(self):
        if not self._lock_token:
            raise NotImplementedError(self.dont_leave_lock_in_place)
        self._leave_lock = False

    def _last_revision_info(self):
        response = self._call('Branch.last_revision_info', self._remote_path())
        if response[0] != 'ok':
            raise SmartProtocolError('unexpected response code %s' % (response,))
        revno = int(response[1])
        last_revision = response[2]
        return (revno, last_revision)

    def _gen_revision_history(self):
        """See Branch._gen_revision_history()."""
        response_tuple, response_handler = self._call_expecting_body(
            'Branch.revision_history', self._remote_path())
        if response_tuple[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response_tuple)
        result = response_handler.read_body_bytes().split('\x00')
        if result == ['']:
            return []
        return result

    def _remote_path(self):
        return self.bzrdir._path_for_remote_call(self._client)

    def _set_last_revision_descendant(self, revision_id, other_branch,
            allow_diverged=False, allow_overwrite_descendant=False):
        # This performs additional work to meet the hook contract; while its
        # undesirable, we have to synthesise the revno to call the hook, and
        # not calling the hook is worse as it means changes can't be prevented.
        # Having calculated this though, we can't just call into
        # set_last_revision_info as a simple call, because there is a set_rh
        # hook that some folk may still be using.
        old_revno, old_revid = self.last_revision_info()
        history = self._lefthand_history(revision_id)
        self._run_pre_change_branch_tip_hooks(len(history), revision_id)
        err_context = {'other_branch': other_branch}
        response = self._call('Branch.set_last_revision_ex',
            self._remote_path(), self._lock_token, self._repo_lock_token,
            revision_id, int(allow_diverged), int(allow_overwrite_descendant),
            **err_context)
        self._clear_cached_state()
        if len(response) != 3 and response[0] != 'ok':
            raise errors.UnexpectedSmartServerResponse(response)
        new_revno, new_revision_id = response[1:]
        self._last_revision_info_cache = new_revno, new_revision_id
        self._run_post_change_branch_tip_hooks(old_revno, old_revid)
        if self._real_branch is not None:
            cache = new_revno, new_revision_id
            self._real_branch._last_revision_info_cache = cache

    def _set_last_revision(self, revision_id):
        old_revno, old_revid = self.last_revision_info()
        # This performs additional work to meet the hook contract; while its
        # undesirable, we have to synthesise the revno to call the hook, and
        # not calling the hook is worse as it means changes can't be prevented.
        # Having calculated this though, we can't just call into
        # set_last_revision_info as a simple call, because there is a set_rh
        # hook that some folk may still be using.
        history = self._lefthand_history(revision_id)
        self._run_pre_change_branch_tip_hooks(len(history), revision_id)
        self._clear_cached_state()
        response = self._call('Branch.set_last_revision',
            self._remote_path(), self._lock_token, self._repo_lock_token,
            revision_id)
        if response != ('ok',):
            raise errors.UnexpectedSmartServerResponse(response)
        self._run_post_change_branch_tip_hooks(old_revno, old_revid)

    @needs_write_lock
    def set_revision_history(self, rev_history):
        # Send just the tip revision of the history; the server will generate
        # the full history from that.  If the revision doesn't exist in this
        # branch, NoSuchRevision will be raised.
        if rev_history == []:
            rev_id = 'null:'
        else:
            rev_id = rev_history[-1]
        self._set_last_revision(rev_id)
        for hook in branch.Branch.hooks['set_rh']:
            hook(self, rev_history)
        self._cache_revision_history(rev_history)

    def _get_parent_location(self):
        medium = self._client._medium
        if medium._is_remote_before((1, 13)):
            return self._vfs_get_parent_location()
        try:
            response = self._call('Branch.get_parent', self._remote_path())
        except errors.UnknownSmartMethod:
            return self._vfs_get_parent_location()
        if len(response) != 1:
            raise errors.UnexpectedSmartServerResponse(response)
        parent_location = response[0]
        if parent_location == '':
            return None
        return parent_location

    def _vfs_get_parent_location(self):
        self._ensure_real()
        return self._real_branch._get_parent_location()

    def set_parent(self, url):
        self._ensure_real()
        return self._real_branch.set_parent(url)

    def _set_parent_location(self, url):
        # Used by tests, to poke bad urls into branch configurations
        if url is None:
            self.set_parent(url)
        else:
            self._ensure_real()
            return self._real_branch._set_parent_location(url)

    def set_stacked_on_url(self, stacked_location):
        """Set the URL this branch is stacked against.

        :raises UnstackableBranchFormat: If the branch does not support
            stacking.
        :raises UnstackableRepositoryFormat: If the repository does not support
            stacking.
        """
        self._ensure_real()
        return self._real_branch.set_stacked_on_url(stacked_location)

    @needs_write_lock
    def pull(self, source, overwrite=False, stop_revision=None,
             **kwargs):
        self._clear_cached_state_of_remote_branch_only()
        self._ensure_real()
        return self._real_branch.pull(
            source, overwrite=overwrite, stop_revision=stop_revision,
            _override_hook_target=self, **kwargs)

    @needs_read_lock
    def push(self, target, overwrite=False, stop_revision=None):
        self._ensure_real()
        return self._real_branch.push(
            target, overwrite=overwrite, stop_revision=stop_revision,
            _override_hook_source_branch=self)

    def is_locked(self):
        return self._lock_count >= 1

    @needs_read_lock
    def revision_id_to_revno(self, revision_id):
        self._ensure_real()
        return self._real_branch.revision_id_to_revno(revision_id)

    @needs_write_lock
    def set_last_revision_info(self, revno, revision_id):
        # XXX: These should be returned by the set_last_revision_info verb
        old_revno, old_revid = self.last_revision_info()
        self._run_pre_change_branch_tip_hooks(revno, revision_id)
        revision_id = ensure_null(revision_id)
        try:
            response = self._call('Branch.set_last_revision_info',
                self._remote_path(), self._lock_token, self._repo_lock_token,
                str(revno), revision_id)
        except errors.UnknownSmartMethod:
            self._ensure_real()
            self._clear_cached_state_of_remote_branch_only()
            self._real_branch.set_last_revision_info(revno, revision_id)
            self._last_revision_info_cache = revno, revision_id
            return
        if response == ('ok',):
            self._clear_cached_state()
            self._last_revision_info_cache = revno, revision_id
            self._run_post_change_branch_tip_hooks(old_revno, old_revid)
            # Update the _real_branch's cache too.
            if self._real_branch is not None:
                cache = self._last_revision_info_cache
                self._real_branch._last_revision_info_cache = cache
        else:
            raise errors.UnexpectedSmartServerResponse(response)

    @needs_write_lock
    def generate_revision_history(self, revision_id, last_rev=None,
                                  other_branch=None):
        medium = self._client._medium
        if not medium._is_remote_before((1, 6)):
            # Use a smart method for 1.6 and above servers
            try:
                self._set_last_revision_descendant(revision_id, other_branch,
                    allow_diverged=True, allow_overwrite_descendant=True)
                return
            except errors.UnknownSmartMethod:
                medium._remember_remote_is_before((1, 6))
        self._clear_cached_state_of_remote_branch_only()
        self.set_revision_history(self._lefthand_history(revision_id,
            last_rev=last_rev,other_branch=other_branch))

    @property
    def tags(self):
        self._ensure_real()
        return self._real_branch.tags

    def set_push_location(self, location):
        self._ensure_real()
        return self._real_branch.set_push_location(location)


def _extract_tar(tar, to_dir):
    """Extract all the contents of a tarfile object.

    A replacement for extractall, which is not present in python2.4
    """
    for tarinfo in tar:
        tar.extract(tarinfo, to_dir)


def _translate_error(err, **context):
    """Translate an ErrorFromSmartServer into a more useful error.

    Possible context keys:
      - branch
      - repository
      - bzrdir
      - token
      - other_branch
      - path

    If the error from the server doesn't match a known pattern, then
    UnknownErrorFromSmartServer is raised.
    """
    def find(name):
        try:
            return context[name]
        except KeyError, key_err:
            mutter('Missing key %r in context %r', key_err.args[0], context)
            raise err
    def get_path():
        """Get the path from the context if present, otherwise use first error
        arg.
        """
        try:
            return context['path']
        except KeyError, key_err:
            try:
                return err.error_args[0]
            except IndexError, idx_err:
                mutter(
                    'Missing key %r in context %r', key_err.args[0], context)
                raise err

    if err.error_verb == 'NoSuchRevision':
        raise NoSuchRevision(find('branch'), err.error_args[0])
    elif err.error_verb == 'nosuchrevision':
        raise NoSuchRevision(find('repository'), err.error_args[0])
    elif err.error_tuple == ('nobranch',):
        raise errors.NotBranchError(path=find('bzrdir').root_transport.base)
    elif err.error_verb == 'norepository':
        raise errors.NoRepositoryPresent(find('bzrdir'))
    elif err.error_verb == 'LockContention':
        raise errors.LockContention('(remote lock)')
    elif err.error_verb == 'UnlockableTransport':
        raise errors.UnlockableTransport(find('bzrdir').root_transport)
    elif err.error_verb == 'LockFailed':
        raise errors.LockFailed(err.error_args[0], err.error_args[1])
    elif err.error_verb == 'TokenMismatch':
        raise errors.TokenMismatch(find('token'), '(remote token)')
    elif err.error_verb == 'Diverged':
        raise errors.DivergedBranches(find('branch'), find('other_branch'))
    elif err.error_verb == 'TipChangeRejected':
        raise errors.TipChangeRejected(err.error_args[0].decode('utf8'))
    elif err.error_verb == 'UnstackableBranchFormat':
        raise errors.UnstackableBranchFormat(*err.error_args)
    elif err.error_verb == 'UnstackableRepositoryFormat':
        raise errors.UnstackableRepositoryFormat(*err.error_args)
    elif err.error_verb == 'NotStacked':
        raise errors.NotStacked(branch=find('branch'))
    elif err.error_verb == 'PermissionDenied':
        path = get_path()
        if len(err.error_args) >= 2:
            extra = err.error_args[1]
        else:
            extra = None
        raise errors.PermissionDenied(path, extra=extra)
    elif err.error_verb == 'ReadError':
        path = get_path()
        raise errors.ReadError(path)
    elif err.error_verb == 'NoSuchFile':
        path = get_path()
        raise errors.NoSuchFile(path)
    elif err.error_verb == 'FileExists':
        raise errors.FileExists(err.error_args[0])
    elif err.error_verb == 'DirectoryNotEmpty':
        raise errors.DirectoryNotEmpty(err.error_args[0])
    elif err.error_verb == 'ShortReadvError':
        args = err.error_args
        raise errors.ShortReadvError(
            args[0], int(args[1]), int(args[2]), int(args[3]))
    elif err.error_verb in ('UnicodeEncodeError', 'UnicodeDecodeError'):
        encoding = str(err.error_args[0]) # encoding must always be a string
        val = err.error_args[1]
        start = int(err.error_args[2])
        end = int(err.error_args[3])
        reason = str(err.error_args[4]) # reason must always be a string
        if val.startswith('u:'):
            val = val[2:].decode('utf-8')
        elif val.startswith('s:'):
            val = val[2:].decode('base64')
        if err.error_verb == 'UnicodeDecodeError':
            raise UnicodeDecodeError(encoding, val, start, end, reason)
        elif err.error_verb == 'UnicodeEncodeError':
            raise UnicodeEncodeError(encoding, val, start, end, reason)
    elif err.error_verb == 'ReadOnlyError':
        raise errors.TransportNotPossible('readonly transport')
    raise errors.UnknownErrorFromSmartServer(err)
