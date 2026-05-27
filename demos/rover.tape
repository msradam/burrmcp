# Records the tutorial GIF (website/public/rover.gif): fast-agent driving the
# rover MCP server. Re-record with:
#   vhs demos/rover.tape
#
# This is a LIVE recording (real Anthropic haiku run, ~18s, a few cents). It
# needs the tutorial scratch setup from the "Build your own agent" guide:
#   ~/theodosia-tutorial-run/rover.py        (the generated state machine)
#   ~/theodosia-tutorial-run/.venv           (theodosia + fast-agent-mcp installed)
#   ANTHROPIC_API_KEY in the repo .env
# The launch (cd, venv, key) is hidden so the capture opens on the agent.

Output website/public/rover.gif

Set Theme "Dracula"
Set FontSize 14
Set Width 1300
Set Height 920
Set Padding 20
Set TypingSpeed 7ms

Hide
Type "cd ~/theodosia-tutorial-run && source .venv/bin/activate >/dev/null 2>&1 && set -a && source /Users/amsrahman/burr-mcp/.env && set +a && export PS1='$ ' && rm -rf .burr && clear"
Enter
Show
Sleep 500ms
Type `fast-agent --no-update-check go --model haiku --stdio "theodosia serve rover:build_application --app-dir ." -m "Drive the rover with the step tool. Terse, no thinking out loud. Your FIRST call must be step(action=deploy_sample_arm); do not power on or run diagnostics first. Then collect one sample and power down. After any refusal, immediately call the action named in valid_next_actions."`
Enter
Sleep 45s
