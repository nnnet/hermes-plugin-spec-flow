#!/usr/bin/env bash
# spec-flow — provision the six role profiles, the shared skills dir and the
# plugin, then lay down per-profile toolset configs.
#
# No absolute paths are baked in: everything derives from HERMES_HOME (default
# ~/.hermes) and the directory this script lives in.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SUITE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # the plugin root
SHARED_SKILLS="$HERMES_HOME/shared-skills"
PLUGINS_DIR="$HERMES_HOME/plugins"

PROFILES=(spec-decomposer spec-contract spec-reviewer implementer verifier researcher)

echo "spec-flow setup"
echo "  HERMES_HOME   = $HERMES_HOME"
echo "  suite dir     = $SUITE_DIR"

# 1. Shared skills — all profiles load them via skills.external_dirs.
mkdir -p "$SHARED_SKILLS"
cp -r "$SUITE_DIR"/skills/* "$SHARED_SKILLS"/
echo "  installed $(ls "$SUITE_DIR/skills" | wc -l) skills -> $SHARED_SKILLS"

# 2. Plugin — deterministic gates + seed tools.
mkdir -p "$PLUGINS_DIR/spec-flow"
cp "$SUITE_DIR"/__init__.py "$SUITE_DIR"/spec_flow_tools.py "$SUITE_DIR"/plugin.yaml "$PLUGINS_DIR/spec-flow"/
echo "  installed plugin -> $PLUGINS_DIR/spec-flow"

# 3. Profiles — create each, then drop its toolset config next to the profile.
for p in "${PROFILES[@]}"; do
  desc_file="$SUITE_DIR/profiles/$p/profile.yaml"
  cfg_file="$SUITE_DIR/profiles/$p/config.yaml"
  desc="$(grep -m1 '^description:' "$desc_file" | sed 's/^description:[[:space:]]*//' || true)"

  if command -v hermes >/dev/null 2>&1; then
    hermes profile create "$p" --description "${desc:-spec-flow $p role}" || \
      echo "  ! profile create $p failed (maybe exists) — continuing"
  else
    echo "  ! hermes binary not found — skipping 'profile create $p' (config still staged)"
  fi

  profile_home="$HERMES_HOME/profiles/$p"
  mkdir -p "$profile_home"
  # Staged as config.yaml.spec-flow so it never clobbers model/keys written by
  # 'profile create'. Merge by hand (or with yq) into the profile's config.yaml.
  cp "$cfg_file" "$profile_home/config.yaml.spec-flow"
  echo "  staged config -> $profile_home/config.yaml.spec-flow"
done

cat <<'EOF'

Done. Next steps (manual, on purpose):
  1. Merge each profiles/<role>/config.yaml.spec-flow into that profile's
     config.yaml (it carries only the tools.cli.enabled/disabled + skills block,
     not your model/keys).
  2. Verify the platform key ('cli') matches the one your dispatcher spawns
     workers under.
  3. Install contract validators if you use contract_check for real:
     redocly / specmatic (OpenAPI), tsc (Zod), buf (Protobuf).
  4. Optional continuous research lane via cron:
       hermes-cron: */30 * * * *  ->  call specflow_revise / research_trigger_check

Run a project:
  specflow_init(project=..., dir=/abs/path)
  specflow_start(goal="Build ...", project=..., dir=/abs/path)
  # then start the gateway dispatcher; the board drives itself.
EOF
