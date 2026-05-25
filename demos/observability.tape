# Observability demo: theodosia sessions + logs, Rose Pine.
# Record with: vhs demos/observability.tape
# Seeds a coffee-order session (one refusal + a clean walk), then shows
# the terminal observability surface. A fixed prompt and generous settle
# time keep the recording legible.

Output demos/observability.gif

Set Shell "bash"
Set Theme "rose-pine"
Set FontSize 16
Set Width 1200
Set Height 380
Set Padding 20
Set TypingSpeed 28ms

# Suppress the venv prompt, set a fixed one, seed a session off-camera.
Hide
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1"
Enter
Type "source .venv/bin/activate >/dev/null 2>&1 || true"
Enter
Type `export PS1="(burr-mcp) > "`
Enter
Type "python examples/scripts/seed_coffee_session.py >/dev/null 2>&1"
Enter
Sleep 4s
Type "clear"
Enter
Show

# List recent sessions.
Type "theodosia sessions ls --project coffee-order-demo --limit 3"
Sleep 600ms
Enter
Sleep 3s
Hide
Type "clear"
Enter
Show

# Full timeline (rich table; red refusal row + green recovery).
Type "theodosia sessions show --project coffee-order-demo"
Sleep 600ms
Enter
Sleep 2s
Screenshot demos/observability_show.png
Sleep 2s
Hide
Type "clear"
Enter
Show

# Compact greppable log, refusals highlighted.
Type "theodosia logs --project coffee-order-demo"
Sleep 600ms
Enter
Sleep 4s
