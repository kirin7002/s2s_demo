# Vendored contract snapshot

Source: StackChan Wall-E `protocol/tools.json` (catalog 0.4.0) and unchanged device wire `protocol/command.schema.json`.
Local source repository: `/Users/kirin/Documents/ChatGPT/stackchan-wall-e`.
These JSON files are copied verbatim from the canonical device repository so the gateway does not depend on a sibling checkout at runtime.
Change the canonical contract first, then update the snapshots together; do not independently edit capability definitions here.
This snapshot includes laughing/scared and the 16-frame, inclusive 100–8000ms limits.
`qwen-tools.json` is the canonical `protocol/examples/qwen-tools.json` example.
`gateway/timing_planner.py`, `gateway/target_planner.py` and `gateway/target_input.py` are verbatim canonical copies.
Normal voice exposes free targets rather than legacy clock-authored perform calls.
The full catalog retains five tools, including legacy perform with default auto
and opt-in strict timing. Both compile into the same bounded, atomic wire command.
