"""Tests for group folder validation and path resolution."""

from __future__ import annotations

import os

import pytest

from codeclaw.group_folder import is_valid_group_folder, resolve_group_folder_path, resolve_group_ipc_path


class TestGroupFolderValidation:
    def test_accepts_normal_names(self):
        assert is_valid_group_folder("main") is True
        assert is_valid_group_folder("family-chat") is True
        assert is_valid_group_folder("Team_42") is True

    def test_rejects_traversal_and_reserved(self):
        assert is_valid_group_folder("../../etc") is False
        assert is_valid_group_folder("/tmp") is False
        assert is_valid_group_folder("global") is False
        assert is_valid_group_folder("") is False


class TestGroupFolderPathResolution:
    def test_resolves_under_groups_directory(self):
        resolved = resolve_group_folder_path("family-chat")
        assert resolved.endswith(os.sep + "groups" + os.sep + "family-chat")

    def test_resolves_ipc_under_data_directory(self):
        resolved = resolve_group_ipc_path("family-chat")
        assert resolved.endswith(
            os.sep + "data" + os.sep + "ipc" + os.sep + "family-chat"
        )

    def test_throws_for_unsafe_folder_names(self):
        with pytest.raises(ValueError):
            resolve_group_folder_path("../../etc")
        with pytest.raises(ValueError):
            resolve_group_ipc_path("/tmp")
