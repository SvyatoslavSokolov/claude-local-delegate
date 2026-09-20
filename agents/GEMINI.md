# Antigravity Delegation & Local Model Rules

## Local Multi-Agent Cluster Integration
You have access to a local GPU cluster (4x RTX 3090 with vLLM / LiteLLM) and Gemini architectures via MCP:
- `mcp__claude-local-delegate__delegate_to_architect`: Dispatch high-level design and decomposition to Tier 1 Gemini Architect.
- `mcp__claude-local-delegate__delegate_to_local`: Dispatch atomic coding/testing tasks to Tier 0 local workers (Qwen 3.8 27B).
- `mcp__claude-local-delegate__show_agent_tree`: Monitor hierarchical execution state.

## Core Rules
1. **Always Anchor `cwd`**: When calling any delegation tool (`delegate_to_local`, `delegate_to_architect`, `delegate_verified`), ALWAYS pass the current project's absolute path in the `cwd` argument. This guarantees that spawned Claude Code agents load the correct `CLAUDE.md`, project skills, and Git context.
2. **Server-Side Wait**: Call `get_delegate_result(run_id, wait_seconds=900)` once to wait for task completion. Avoid tight polling loops with `check_delegate_status`.
3. **Disjoint Concurrency**: Do not run parallel workers on overlapping files.

## Architectural Alignment & Reliability (2026-09)
4. **Control-Data Flow Separation**: Keep natural language reasoning (the "Control" flow) STRICTLY SEPARATE from your final code/data artifacts (the "Data" flow). Use clear markdown block delimiters for artifacts so the Tier 1 Architect can predictably parse your output. Do not interleave conversational text with final code outputs.
5. **Contrastive Chain-of-Thought**: For complex logic changes, explicitly state the naive/wrong approach first, explain why it fails (edge cases, bounds), and then provide the correct solution.
6. **Executable Verifiers**: Never rely on LLM-as-a-judge to determine if your fix works. Prove correctness via runtime. Write a short Python/Bash test script to explicitly validate your artifact before declaring it done.
7. **Error Clustering & Delegation Refinement (AgentGrad)**: When local workers fail repeatedly, cluster their errors, extract the root cause, and formulate a specific "Anti-Pattern / Correct Pattern" rule. Inject this specific rule into the next `delegate_to_local` call.
8. **Cost-Aware Routing (ProgRouter)**: Use the local Qwen 3.8 27B workers for 80-90% of routine coding and searches. Retain complex architectural synthesis for yourself (Tier 1).
9. **Same-Designer Confound (Cross-Family Verification)**: Qwen 3.8 27B must NEVER evaluate the semantic correctness of its own code. It must use purely execution-based verification. Semantic validation must be performed by Tier 1 (Gemini).
10. **Contagion Recovery (Safe-Kill)**: Local agents can corrupt their context if they veer off course. Do not waste tokens trying to debug a worker stuck in a loop (>3 failures). Kill it and spawn a fresh instance. Do not allow parallel agents to share mutable state.
