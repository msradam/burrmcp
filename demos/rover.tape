# Records the raw capture for the tutorial GIF: fast-agent driving the rover MCP
# server. Re-record with:
#   vhs demos/rover.tape
#
# This is a LIVE recording (real Anthropic haiku run, a few cents). It needs the
# tutorial scratch setup from the "Build your own agent" guide:
#   ~/theodosia-tutorial-run/rover.py        (the generated state machine)
#   ~/theodosia-tutorial-run/.venv           (theodosia + fast-agent-mcp installed)
#   ANTHROPIC_API_KEY in the repo .env
# The launch (cd, venv, key) is hidden so the capture opens on the agent.
#
# This tape writes the full-length raw capture. The shipped website/public/rover.gif
# is a post-processed cut of it: the action window (deploy refusal + recovery
# through power-down) slowed ~1.25x for readability, a hold on the final summary,
# scaled to 900px and palette-reduced. Find the action window in the raw capture
# (it shifts with model latency) and re-run the ffmpeg trim/concat used to build it.

Output /tmp/rover_raw.gif

Set Theme "Dracula"
Set FontSize 24
Set Width 1120
Set Height 720
Set Padding 18
Set TypingSpeed 6ms

Hide
Type "cd ~/theodosia-tutorial-run && source .venv/bin/activate >/dev/null 2>&1 && set -a && source /Users/amsrahman/burr-mcp/.env && set +a && export PS1='$ ' && rm -rf .burr && clear"
Enter
Show
Sleep 500ms
Type `fast-agent --no-update-check go --model haiku --stdio "theodosia serve rover:build_application --app-dir ." -m "Drive the rover with the step tool. Terse, no thinking out loud. Your FIRST call must be step(action=deploy_sample_arm); do not power on or run diagnostics first. Then collect one sample and power down. After any refusal, immediately call the action named in valid_next_actions."`
Enter
Sleep 45s
