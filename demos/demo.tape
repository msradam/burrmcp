# Records the README hero GIF (demos/demo.gif). Re-record with:
#   vhs demos/demo.tape
#
# Replays the captured trace from examples/scripts/demo_recorded_trace.txt
# (a real Claude Agent SDK run against the incident-response MCP server).
# To refresh the trace from a fresh Claude session (costs ~$0.50):
#   uv run python examples/scripts/demo_walk.py > examples/scripts/demo_recorded_trace.txt
#
# The launch is hidden and demo_play.py clears the screen on start, so the
# capture opens on the agent transcript rather than a Python command.

Output demos/demo.gif

Set Theme "Dracula"
Set FontSize 15
Set Width 1300
Set Height 820
Set Padding 20
Set TypingSpeed 20ms

Hide
Type "source .venv/bin/activate >/dev/null 2>&1 || true"
Enter
Type ".venv/bin/python examples/scripts/demo_play.py"
Enter
Sleep 500ms
Show
Sleep 14s
