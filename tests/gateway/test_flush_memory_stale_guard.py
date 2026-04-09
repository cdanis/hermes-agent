"""Tests for deferred skill/memory drafting via cron job (#2670).

Verifies that:
1. Cron sessions are skipped (no flush for headless cron runs)
2. Memory state is included in the scheduled cron job prompt
3. The cron job is properly configured with correct schedule and skill
"""

import sys
import types
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call


@pytest.fixture(autouse=True)
def _mock_dotenv(monkeypatch):
    """gateway.run imports dotenv at module level; stub it so tests run without the package."""
    fake = types.ModuleType("dotenv")
    fake.load_dotenv = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "dotenv", fake)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner.adapters = {}
    runner.hooks = MagicMock()
    runner.session_store = MagicMock()
    return runner


_TRANSCRIPT_4_MSGS = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi there"},
    {"role": "user", "content": "remember my name is Alice"},
    {"role": "assistant", "content": "Got it, Alice!"},
]


class TestCronSessionBypass:
    """Cron sessions should never trigger a memory flush."""

    def test_cron_session_skipped(self):
        runner = _make_runner()
        runner._flush_memories_for_session("cron_job123_20260323_120000")
        # session_store.load_transcript should never be called
        runner.session_store.load_transcript.assert_not_called()

    def test_cron_session_with_prefix_skipped(self):
        """Cron sessions with different prefixes are still skipped."""
        runner = _make_runner()
        runner._flush_memories_for_session("cron_daily_20260323")
        runner.session_store.load_transcript.assert_not_called()

    def test_non_cron_session_proceeds(self):
        """Non-cron sessions should still attempt to schedule the flush."""
        runner = _make_runner()
        runner.session_store.load_transcript.return_value = []
        with patch("cron.create_job"):
            runner._flush_memories_for_session("session_abc123")
        runner.session_store.load_transcript.assert_called_once_with("session_abc123")


class TestMemoryInjection:
    """Memory state should be included in the scheduled cron job prompt."""

    def test_memory_content_included_in_cron_prompt(self, tmp_path, monkeypatch):
        """When memory files exist, their content appears in the scheduled cron job prompt."""
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        (memory_dir / "MEMORY.md").write_text("Agent knows Python\nUser prefers dark mode")
        (memory_dir / "USER.md").write_text("Name: Alice\nTimezone: PST")

        runner = _make_runner()
        runner.session_store.load_transcript.return_value = _TRANSCRIPT_4_MSGS

        with (
            patch("cron.create_job") as mock_create_job,
            patch.dict("sys.modules", {"tools.memory_tool": MagicMock(get_memory_dir=lambda: memory_dir)}),
        ):
            runner._flush_memories_for_session("session_123")

        # Should schedule a cron job, not run synchronously
        mock_create_job.assert_called_once()
        
        # Check the prompt includes memory content
        call_kwargs = mock_create_job.call_args.kwargs
        assert "session_id: session_123" in call_kwargs.get("prompt", "")
        assert "Agent knows Python" in call_kwargs.get("prompt", "")
        assert "User prefers dark mode" in call_kwargs.get("prompt", "")
        assert "Name: Alice" in call_kwargs.get("prompt", "")
        assert "Timezone: PST" in call_kwargs.get("prompt", "")
        assert "skill-saver" in call_kwargs.get("skill", "")
        assert call_kwargs.get("schedule") == "30m"
        assert call_kwargs.get("repeat") == 1  # Run once only

    def test_flush_works_without_memory_files(self, tmp_path, monkeypatch):
        """When no memory files exist, cron job still schedules without the guard."""
        empty_dir = tmp_path / "no_memories"
        empty_dir.mkdir()

        runner = _make_runner()
        runner.session_store.load_transcript.return_value = _TRANSCRIPT_4_MSGS

        with (
            patch("cron.create_job") as mock_create_job,
            patch.dict("sys.modules", {"tools.memory_tool": MagicMock(get_memory_dir=lambda: empty_dir)}),
        ):
            runner._flush_memories_for_session("session_456")

        # Should still schedule the cron job
        mock_create_job.assert_called_once()
        call_kwargs = mock_create_job.call_args.kwargs
        assert "(No existing memory found)" in call_kwargs.get("prompt", "")
        assert "skill-saver" in call_kwargs.get("skill", "")


class TestCronJobConfiguration:
    """Verify the cron job is configured correctly."""

    def test_correct_schedule_and_skill(self, monkeypatch):
        """The scheduled job should use 30m delay and load skill-saver."""
        runner = _make_runner()
        runner.session_store.load_transcript.return_value = _TRANSCRIPT_4_MSGS

        with (
            patch("cron.create_job") as mock_create_job,
            patch.dict("sys.modules", {"tools.memory_tool": MagicMock(get_memory_dir=lambda: Path("/nonexistent"))}),
        ):
            runner._flush_memories_for_session("session_test")

        mock_create_job.assert_called_once()
        call_kwargs = mock_create_job.call_args.kwargs
        
        assert call_kwargs.get("schedule") == "30m"
        assert call_kwargs.get("skill") == "skill-saver"
        assert call_kwargs.get("repeat") == 1
        assert call_kwargs.get("deliver") == "origin"
        assert call_kwargs.get("name", "").startswith("Skill/Memory draft:")

    def test_prompt_includes_skill_format_guidance(self, monkeypatch):
        """The prompt should include explicit skill format requirements."""
        runner = _make_runner()
        runner.session_store.load_transcript.return_value = _TRANSCRIPT_4_MSGS

        with (
            patch("cron.create_job") as mock_create_job,
            patch.dict("sys.modules", {"tools.memory_tool": MagicMock(get_memory_dir=lambda: Path("/nonexistent"))}),
        ):
            runner._flush_memories_for_session("session_test")

        prompt = mock_create_job.call_args.kwargs.get("prompt", "")
        
        # Should have format instructions (these are already in the code)
        assert "session_id:" in prompt
        assert "skill-saver" in prompt
        assert "YAML frontmatter" in prompt
        assert "slas" in prompt.lower()  # "slas" matches "slashes"


class TestPromptStructure:
    """Verify the scheduled prompt retains core instructions."""

    def test_core_instructions_present(self, monkeypatch):
        """The scheduled prompt should still contain the original guidance."""
        runner = _make_runner()
        runner.session_store.load_transcript.return_value = _TRANSCRIPT_4_MSGS

        with (
            patch("cron.create_job") as mock_create_job,
            patch.dict("sys.modules", {"tools.memory_tool": MagicMock(get_memory_dir=lambda: Path("/nonexistent"))}),
        ):
            runner._flush_memories_for_session("session_struct")

        prompt = mock_create_job.call_args.kwargs.get("prompt", "")
        assert "session_id:" in prompt
        assert "memory or as a skill" in prompt
        assert "If nothing is worth saving, just skip" in prompt
