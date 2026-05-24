# Observability demo: burrmcp sessions + logs, Rose Pine.
# Record with: vhs demos/observability.tape
# Seeds a coffee-order session (one refusal + a clean walk), then shows
# the terminal observability surface.

Output demos/observability.gif

Set Theme "rose-pine"
Set FontSize 13
Set Width 1180
Set Height 840
Set Padding 22
Set TypingSpeed 28ms

# Activate the venv + seed a session off-camera.
Hide
Type "source .venv/bin/activate"
Enter
Type "python examples/scripts/seed_coffee_session.py >/dev/null 2>&1"
Enter
Sleep 4s
Type "clear"
Enter
Show

# List recent sessions.
Type "burrmcp sessions ls --project coffee-order-demo --limit 3"
Sleep 600ms
Enter
Sleep 3s

# Full timeline (rich table; red refusal row + green recovery).
Type "burrmcp sessions show --project coffee-order-demo"
Sleep 600ms
Enter
Sleep 4s
Screenshot demos/observability_show.png

# Compact greppable log, refusals highlighted.
Type "burrmcp logs --project coffee-order-demo"
Sleep 600ms
Enter
Sleep 4s
