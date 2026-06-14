#!/usr/bin/env bash
# Live-run LLM config — source this before tests/run_cases.py:
#
#     source tests/live-run.env.sh
#     python3 tests/run_cases.py --case p4 --decomposer llm --implementer llm \
#         --depth execute --model "$SPEC_FLOW_LLM_MODEL" \
#         --dashboard --dashboard-port 8092
#
# Pick ONE variant below. Bifrost config is never touched — the tests only
# choose where to send the OpenAI-style request and which model to name.

# ── Variant A (ACTIVE): OpenRouter THROUGH Bifrost ───────────────────────────
# claude-style agents → llm_backend(openai) → Bifrost unified endpoint →
# Bifrost's own "openrouter" provider (key lives in Bifrost). The model name
# carries the provider prefix; Bifrost strips it and routes.
export SPEC_FLOW_LLM_BACKEND=openai
export SPEC_FLOW_LLM_BASE_URL="${SPEC_FLOW_BIFROST_URL_OPENAI:-http://127.0.0.1:8080/v1}"
unset SPEC_FLOW_LLM_API_KEY        # local Bifrost needs no bearer
export SPEC_FLOW_LLM_MODEL="openrouter/openai/gpt-oss-120b:free"

# ── Variant B (commented): OpenRouter DIRECT (own key from the host env) ─────
# export SPEC_FLOW_LLM_BACKEND=openai
# export SPEC_FLOW_LLM_BASE_URL=https://openrouter.ai/api/v1
# export SPEC_FLOW_LLM_API_KEY="$OPENROUTER_API_KEY"
# export SPEC_FLOW_LLM_MODEL="openai/gpt-oss-120b:free"

# ── Variant C (commented): claude CLI (default backend; Anthropic quota) ─────
# routing to headroom/bifrost happens via ANTHROPIC_BASE_URL as usual
# export SPEC_FLOW_LLM_BACKEND=claude
# export SPEC_FLOW_LLM_MODEL=haiku

# ── shared knobs (all variants) ──────────────────────────────────────────────
export SPEC_FLOW_LLM_RETRIES=6              # free pools throttle; be patient
# flexible atomicity: the forced-leaf safety net sits far out at depth 6 —
# the model's atomic judgment + leaf_check decide the real tree shape
export SPEC_FLOW_LLM_LEAF_DEPTH=6
export SPEC_FLOW_LLM_MAX_CHILDREN=4
export SPEC_FLOW_MAX_DECOMPOSE_CALLS=160

# creator ensemble: generate N implementer candidates from different free
# models, keep the first that compiles clean (catches weak-model non-ASCII /
# syntax before integrate). 2 = the two free models in the implementer chain.
export SPEC_FLOW_CREATOR_ENSEMBLE="${SPEC_FLOW_CREATOR_ENSEMBLE:-2}"
