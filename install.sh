#!/usr/bin/env bash
#
# Wire ormeggio into ~/.claude without the plugin marketplace.
#
# The marketplace is the short way (see the README). This is for a clone you
# want to keep and update with `git pull`.
#
# The skill goes in as a symlink rather than a copy, so a pull updates what is
# installed and no second copy exists to drift away from this one. The hooks
# cannot be symlinked, because they are not files in a folder Claude Code scans:
# they are entries inside settings.json. So the entries carry the absolute path
# of the script in this repo, which gives them the same property.
#
# settings.json is shared with every other tool that registers a hook, so this
# script reads it, adds only what is missing, and writes it back. It never
# generates one from scratch over the top of what is there.
#
# Dry run by default. Only `./install.sh --apply` writes anything.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
SETTINGS="${CLAUDE_DIR}/settings.json"
APPLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    -h|--help)
      cat <<EOF
usage: ./install.sh [--apply]

Wires ormeggio into ~/.claude: the ormeggio skill, and the two hooks that notice
a long absence and record failed turns.

Without --apply it prints what it would do and writes nothing.
EOF
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      echo "usage: ./install.sh [--apply]" >&2
      exit 2
      ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is missing, and both ormeggio hooks run a python3 script." >&2
  exit 1
}

if [ ! -d "$CLAUDE_DIR" ]; then
  cat >&2 <<EOF
$CLAUDE_DIR does not exist, so there is nothing to install into.

That folder is made by Claude Code the first time it runs. Install Claude Code
and start it once, then run this again.
EOF
  exit 1
fi

if [ ! -f "$REPO/hooks/hooks.json" ]; then
  echo "$REPO/hooks/hooks.json is missing, so the hooks cannot be read." >&2
  exit 1
fi

# The hook entries are not written out here. They are read from hooks/hooks.json,
# the same file the plugin install path uses, with CLAUDE_PLUGIN_ROOT replaced by
# this repo. One source of truth, so a hook added there arrives here without
# anybody remembering to edit two files.
#
# Modes: check parses and merges in memory and prints nothing, plan prints what
# it would do, apply backs the file up and writes it.
merge_hooks() {
  python3 - "$1" "$SETTINGS" "$REPO" <<'PY'
import json
import os
import shutil
import sys
import time

mode, settings_path, repo = sys.argv[1], sys.argv[2], sys.argv[3]
quiet = mode == "check"

def stop(message):
    sys.stderr.write(message.rstrip() + "\n")
    raise SystemExit(2)

def say(line):
    if not quiet:
        print(line)

# Whatever is on disk now. A missing file is a fresh install. A file that does
# not parse is somebody else's half written edit, and guessing at it would lose
# their work, so it stops here instead.
raw = ""
if os.path.exists(settings_path):
    with open(settings_path, encoding="utf-8") as fh:
        raw = fh.read()

if raw.strip():
    try:
        settings = json.loads(raw)
    except ValueError as e:
        stop(
            f"{settings_path} is not valid JSON ({e}).\n"
            "Nothing was changed. Other tools in this suite write to that same file, "
            "so repair it, or restore one of the .bak- copies next to it, then run this again."
        )
    if not isinstance(settings, dict):
        stop(f"{settings_path} holds a {type(settings).__name__}, and settings.json has to be an object. Nothing was changed.")
else:
    settings = {}

with open(os.path.join(repo, "hooks", "hooks.json"), encoding="utf-8") as fh:
    wanted = json.load(fh)["hooks"]

def resolve(command):
    return command.replace("${CLAUDE_PLUGIN_ROOT}", repo).replace("$CLAUDE_PLUGIN_ROOT", repo)

hooks = settings.get("hooks", {})
if not isinstance(hooks, dict):
    stop(f'the "hooks" value in {settings_path} is a {type(hooks).__name__} and has to be an object. Nothing was changed.')

def matcher_of(group):
    m = group.get("matcher")
    return m if isinstance(m, str) else ""

added = 0
stale = []
ours = {os.path.join(repo, "skills", "ormeggio", "scripts", "ormeggio.py")}

for event, groups in wanted.items():
    existing = hooks.get(event, [])
    if not isinstance(existing, list):
        stop(f"hooks.{event} in {settings_path} is a {type(existing).__name__} and has to be a list. Nothing was changed.")

    # A hook naming ormeggio's script from some other folder is left over from
    # a copy of this repo that has moved. Reported, never removed: it might be a
    # second checkout somebody runs on purpose.
    for group in existing:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            cmd = hook.get("command", "") if isinstance(hook, dict) else ""
            for name in ("scripts/ormeggio.py",):
                if name in cmd and not any(o in cmd for o in ours):
                    stale.append((event, cmd))

    for wanted_group in groups:
        wanted_match = matcher_of(wanted_group)
        for wanted_hook in wanted_group.get("hooks", []):
            command = resolve(wanted_hook["command"])
            where = f"{event} ({wanted_match})" if wanted_match else event

            already = any(
                isinstance(h, dict) and h.get("command") == command
                for g in existing if isinstance(g, dict)
                for h in (g.get("hooks") or [])
            )
            if already:
                say(f"  hook   {where}\n         {command}\n         [already registered]")
                continue

            # Same matcher joins the group that is already there. A different
            # matcher gets its own group, so ormeggio never narrows or widens a
            # hook somebody else registered.
            target = next(
                (g for g in existing if isinstance(g, dict) and matcher_of(g) == wanted_match),
                None,
            )
            if target is None:
                # matcher first, then hooks, the order every other group in the
                # file already uses. A diff of settings.json is read by people.
                target = {"matcher": wanted_match, "hooks": []} if wanted_match else {"hooks": []}
                existing.append(target)
            entry = dict(wanted_hook)
            entry["command"] = command
            target.setdefault("hooks", []).append(entry)
            added += 1
            say(f"  hook   {where}\n         {command}\n         [{'added' if mode == 'apply' else 'would add'}]")

    hooks[event] = existing

for event, command in stale:
    say(f"  note   an older ormeggio hook on {event} points somewhere else:\n         {command}\n         [left alone, delete it by hand if that copy is gone]")

if mode != "apply":
    raise SystemExit(0)

if not added:
    say("  settings.json already holds both hooks, so it was not touched and no backup was made.")
    raise SystemExit(0)

# A copy first, always, and say where it went. This file is shared, and an
# installer that eats somebody else's hooks with no way back is worse than one
# that refuses to run.
if os.path.exists(settings_path):
    backup = f"{settings_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(settings_path, backup)
    say(f"  backup {backup}")

settings["hooks"] = hooks
tmp = settings_path + ".ormeggio-tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(settings, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, settings_path)
say(f"  wrote  {settings_path}")
PY
}

link_plan() {
  src="$1"
  dest="$2"
  label="$3"
  echo "  $label $dest"

  if [ -L "$dest" ]; then
    current="$(readlink "$dest")"
    echo "         -> $current"
    if [ "$current" = "$src" ]; then
      echo "         [already linked]"
      return 0
    fi
    echo "         [a symlink is there and it points somewhere else]"
    return 1
  fi

  if [ -e "$dest" ]; then
    echo "         [a real file or folder is there, and ormeggio did not put it there]"
    return 1
  fi

  echo "         -> $src"
  if [ "$APPLY" = "1" ]; then
    mkdir -p "$(dirname "$dest")"
    ln -s "$src" "$dest"
    echo "         [linked]"
  else
    echo "         [would link]"
  fi
  return 0
}

# settings.json is parsed and merged in memory before a single symlink is made,
# so a file that cannot be read leaves the machine exactly as it was.
merge_hooks check

if [ "$APPLY" = "1" ]; then
  echo "ormeggio installer, writing to $CLAUDE_DIR"
else
  echo "ormeggio installer, dry run. Nothing will be written."
fi
echo "  repo   $REPO"
echo

conflict=0
link_plan "$REPO/skills/ormeggio" "$CLAUDE_DIR/skills/ormeggio" "skill " || conflict=1

if [ "$conflict" = "1" ]; then
  echo
  echo "Something is installed under that name and ormeggio did not put it there." >&2
  echo "Move it or delete it, then run this again. settings.json was not touched." >&2
  exit 1
fi

echo

if [ "$APPLY" != "1" ]; then
  plan="$(merge_hooks plan)"
  echo "$plan"
  echo
  if echo "$plan" | grep -q "would add"; then
    echo "Nothing was written. Run ./install.sh --apply to do it."
  else
    echo "Nothing to do, both hooks are registered already."
  fi
  exit 0
fi

merge_hooks apply

echo
echo "Done. The next message you send after a pause longer than your threshold"
echo "(24 h until you measure your own) gets a paragraph about your sessions."
echo
echo "To set the threshold from your own pauses:"
echo "  python3 $REPO/skills/ormeggio/scripts/ormeggio.py threshold --save"
echo
echo "To undo it:"
echo "  rm $CLAUDE_DIR/skills/ormeggio"
echo "  then take ormeggio's two entries out of the hooks block in $SETTINGS"
echo "  (the .bak- copy next to that file is how it looked before this run)"
