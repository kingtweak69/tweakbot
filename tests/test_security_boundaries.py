
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(name):
    return (ROOT / name).read_text(encoding="utf-8")

def test_oauth_has_no_database_persistence():
    db = read("utils/database.py")
    assert "CREATE TABLE IF NOT EXISTS linked_accounts" not in db
    assert "set_linked_account" not in db
    assert "get_linked_account" not in db
    for name in ("cogs/github.py", "cogs/railway.py"):
        text = read(name)
        assert "set_linked_account" not in text
        assert "get_linked_account" not in text

def test_oauth_vault_is_process_local():
    text = read("utils/credentials.py")
    assert "TOKEN_ENCRYPTION_KEY" not in text
    assert "Fernet" not in text
    assert "asyncio.Lock" in text
    assert "dict[tuple[int, str], dict[str, Any]]" in text

def test_destructive_ai_tools_are_disabled_by_default():
    cfg = read("config.py")
    assert 'AI_DESTRUCTIVE_TOOLS_ENABLED",\n    False' in cfg
    caps = read("utils/capabilities.py")
    assert "capability.destructive" in caps
    assert "AI_DESTRUCTIVE_TOOLS_ENABLED" in caps

def test_workspace_is_configurable_for_durable_storage():
    text = read("utils/workspace.py")
    assert "AGENT_WORKSPACE_ROOT" in text
    assert "/data/tweakbot-workspaces" in text
    db = read("utils/database.py")
    assert "CREATE TABLE IF NOT EXISTS agent_workspaces" in db

def test_audit_telemetry_redacts_credentials():
    text = read("utils/capabilities.py")
    assert "_audit_safe" in text
    assert "_SECRET_KEY_RE" in text
    assert "_TOKEN_RE" in text

def test_persistent_agent_state_redacts_tool_outputs():
    text = read("cogs/agent_jobs.py")
    assert "from utils.capabilities import _audit_safe" in text
    assert "result=_audit_safe(outcome)" in text
    assert "arguments=_audit_safe(args)" in text

def test_railway_logs_are_redacted_before_output():
    text = read("cogs/railway.py")
    assert "_redact_log_secrets" in text
    assert "[REDACTED_RAILWAY_SECRET]" in text
