# Copyright (C) 2006 Jelmer Vernooij <jelmer@samba.org>

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

from bzrlib.bzrdir import BzrDir
from bzrlib.errors import NoSuchRevision
from bzrlib.inventory import Inventory
from bzrlib.workingtree import WorkingTree

import os
import svn
import format
import workingtree
from tests import TestCaseWithSubversionRepository

class TestWorkingTree(TestCaseWithSubversionRepository):
    def test_add_duplicate(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data"})
        self.client_add("dc/bl")
        tree = WorkingTree.open("dc")
        tree.add(["bl"])

    def test_add(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data"})
        tree = WorkingTree.open("dc")
        tree.add(["bl"])

        inv = tree.read_working_inventory()
        self.assertIsInstance(inv, Inventory)
        self.assertTrue(inv.has_filename("bl"))
        self.assertFalse(inv.has_filename("aa"))

    def test_remove(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data"})
        tree = WorkingTree.open("dc")
        tree.add(["bl"])
        tree.remove(["bl"])
        inv = tree.read_working_inventory()
        self.assertFalse(inv.has_filename("bl"))

    def test_remove_dup(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data"})
        tree = WorkingTree.open("dc")
        tree.add(["bl"])
        os.remove("dc/bl")
        inv = tree.read_working_inventory()
        self.assertFalse(inv.has_filename("bl"))

    def test_is_control_file(self):
        self.make_client_and_bzrdir('a', 'dc')
        tree = WorkingTree.open("dc")
        self.assertTrue(tree.is_control_filename(".svn"))
        self.assertFalse(tree.is_control_filename(".bzr"))

    def test_revert(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data"})
        self.client_add("dc/bl")
        self.client_commit("dc", "Bla")
        tree = WorkingTree.open("dc")
        os.remove("dc/bl")
        tree.revert(["bl"])
        self.assertEqual("data", open('dc/bl').read())

    def test_rename_one(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data"})
        self.client_add("dc/bl")
        self.client_commit("dc", "Bla")
        tree = WorkingTree.open("dc")
        tree.rename_one("bl", "bloe")
        
        basis_inv = tree.basis_tree()
        inv = tree.read_working_inventory()
        self.assertFalse(inv.has_filename("bl"))
        self.assertTrue(inv.has_filename("bloe"))
        self.assertEqual(basis_inv.path2id("bl"), 
                         inv.path2id("bloe"))
        self.assertIs(None, inv.has_filename("bl"))
        self.assertIs(None, basis_inv.has_filename("bloe"))

    def test_move(self):
        self.make_client_and_bzrdir('a', 'dc')
        self.build_tree({"dc/bl": "data", "dc/a": "data2", "dc/dir": None})
        self.client_add("dc/bl", "dc/a", "dc/dir")
        self.client_commit("dc", "Bla")
        tree = WorkingTree.open("dc")
        tree.move(["bl", "a"], "dir")
        
        basis_inv = tree.basis_tree()
        inv = tree.read_working_inventory()
        self.assertFalse(inv.has_filename("bl"))
        self.assertFalse(inv.has_filename("a"))
        self.assertTrue(inv.has_filename("dir/bl"))
        self.assertTrue(inv.has_filename("dir/a"))
        self.assertEqual(basis_inv.path2id("bl"), 
                         inv.path2id("dir/bl"))
        self.assertEqual(basis_inv.path2id("a"), 
                         inv.path2id("dir/a"))
        self.assertIs(None, inv.has_filename("bl"))
        self.assertIs(None, basis_inv.has_filename("dir/bl"))

