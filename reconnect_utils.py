def should_skip_previous_session_kick(init_data):
    """Return True when reconnecting to the same browser session should not force a fresh session."""
    return bool(init_data.get("reconnect")) and bool(init_data.get("session_id"))
