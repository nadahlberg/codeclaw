"""Tests for container runtime operations."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from codeclaw.container_runtime import (
    CONTAINER_RUNTIME_BIN,
    cleanup_orphans,
    ensure_container_runtime_running,
    readonly_mount_args,
    stop_container_cmd,
)


class TestReadonlyMountArgs:
    def test_returns_v_flag_with_ro(self):
        args = readonly_mount_args("/host/path", "/container/path")
        assert args == ["-v", "/host/path:/container/path:ro"]


class TestStopContainer:
    def test_returns_stop_command(self):
        assert stop_container_cmd("codeclaw-test-123") == (
            f"{CONTAINER_RUNTIME_BIN} stop codeclaw-test-123"
        )


class TestEnsureContainerRuntimeRunning:
    @patch("codeclaw.container_runtime.subprocess.run")
    def test_does_nothing_when_running(self, mock_run):
        mock_run.return_value = None  # No exception = success
        ensure_container_runtime_running()
        mock_run.assert_called_once()

    @patch("codeclaw.container_runtime.subprocess.run")
    def test_raises_when_docker_info_fails(self, mock_run):
        mock_run.side_effect = FileNotFoundError("Cannot connect to the Docker daemon")
        with pytest.raises(RuntimeError, match="Container runtime is required"):
            ensure_container_runtime_running()


class TestCleanupOrphans:
    @patch("codeclaw.container_runtime.subprocess.run")
    def test_stops_orphaned_containers(self, mock_run):
        # First call: docker ps returns container names
        mock_run.return_value = type("Result", (), {
            "stdout": "codeclaw-group1-111\ncodeclaw-group2-222\n"
        })()
        cleanup_orphans()
        # ps + 2 stop calls = 3
        assert mock_run.call_count == 3

    @patch("codeclaw.container_runtime.subprocess.run")
    def test_does_nothing_when_no_orphans(self, mock_run):
        mock_run.return_value = type("Result", (), {"stdout": ""})()
        cleanup_orphans()
        assert mock_run.call_count == 1

    @patch("codeclaw.container_runtime.subprocess.run")
    def test_continues_when_ps_fails(self, mock_run):
        mock_run.side_effect = Exception("docker not available")
        cleanup_orphans()  # Should not raise
