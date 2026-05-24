# Live observability: burrmcp watch, Rose Pine.
# Record with: vhs demos/watch.tape
# Backgrounds a paced coffee-order walk, then live-tails it: steps appear
# in real time, a red refusal then green recovery.

Output demos/watch.gif

Set Theme "rose-pine"
Set FontSize 13
Set Width 1180
Set Height 720
Set Padding 22
Set TypingSpeed 28ms

Hide
Type "source .venv/bin/activate"
Enter
# Start a paced walk in the background; give it a beat to create the session.
Type "python examples/scripts/seed_coffee_session.py --slow >/dev/null 2>&1 &"
Enter
Sleep 1500ms
Type "clear"
Enter
Show

Type "burrmcp watch --project coffee-order-demo"
Sleep 600ms
Enter
# Watch animates as the background walk writes a step every ~2s.
Sleep 15s
Screenshot demos/watch_frame.png
Ctrl+C
