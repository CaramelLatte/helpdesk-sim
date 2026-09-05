Help Desk Simulation Trainer — Project Context

What this is

A local, self-hosted training environment that simulates the full lifecycle of an IT help desk ticket: a stateful AI persona files and responds to a ticket like a real (variably patient, variably technical) end user, the operator works the ticket — including running diagnostic commands against a simulated shell — and a separate grading model evaluates the operator's performance afterward.

This is a portfolio/capstone project. Its purpose is to demonstrate, in an interview-defensible way, the skills that entry-level help desk / SOC postings actually screen for but rarely test directly: ticket communication quality under a realistic lifecycle, correct diagnostic sequencing, and — critically — knowing when to escalate rather than always "solving it yourself."

Every architectural decision below has already been made deliberately. Don't relitigate them without being asked; if something here seems wrong, flag it rather than silently working around it.

Hardware / environment


Framework Desktop, AMD AI Max 395+ APU, 128GB unified RAM (~96GB addressable to a single model)
Existing homelab stack: personal site (Express + Nginx), AI inference server (LM Studio), Voxta server, Docker-based game server orchestration, Sunshine streaming, NAS with 10GbE direct-connect, all unified under Tailscale
This project should integrate into that stack, not duplicate or conflict with it (e.g. reuse Nginx/Docker conventions already in place rather than inventing new ones)


Core architecture — two-model split

Persona model (small, fast, local): plays the ticket-filer. Needs to run at high turn frequency without starving the rest of the inference server, especially once concurrent tickets or the shell-sim piece are active. Target range: Llama 3.1 8B / Phi-4 / Qwen2.5 7B–14B class.

Grading model (largest comfortably-fitting, local): evaluates the operator's performance after a ticket closes (or at defined milestones). Runs less frequently, can tolerate more latency — this is where the VRAM/quality budget should go.

Do not collapse these into one model "for simplicity." The split is intentional: persona needs speed and consistency, grading needs reasoning depth.

Persona system


Persona schema, not just a system prompt. Traits (patience, technical literacy, verbosity, formality, mood-volatility, etc.) as numeric scales, randomly rolled at ticket creation and stored alongside the ticket — not re-derived from the prompt each turn.
State-aware, proactively engaging. The persona must react to elapsed state (e.g. "still broken!" several replies later), not just generate once and go quiet.
Mood as a state machine, not a one-shot tag. E.g. calm → waiting → impatient → escalated, with transitions driven by signals like elapsed time since last response, number of clarifying questions asked, or the persona model emitting a "felt unheard" signal.
Different personality blends should have different iteration/prompting cadence — an impatient persona checks in more often; this should be a rule applied on top of the mood state, not hardcoded per-persona.


Ticketing platform: Zammad

Self-hosted Zammad, chosen over osTicket (API too limited), GLPI (inconsistent field naming, incomplete API docs), and commercial options like Freshservice (trial limits, licensing complexity). Reasons: modern REST API, AGPL/no licensing cost, Docker-first setup, API coverage depth.

Explicitly out of scope: chasing PSA feature parity (billing, contracts, multi-client management). Zammad is the anchor because it's a real, well-documented ticketing API — not because this project is trying to be a full PSA replacement.

Simulated shell / diagnostic component

A second orchestration endpoint that accepts text commands (think "fake SSH session") and returns deterministic, scenario-aware terminal output, advancing a state machine as the operator runs commands.

Critical design decision: this is rule-based and templated, NOT LLM-generated at runtime. Reasons (do not relitigate):


Determinism matters for fairness — the same command against the same scenario state must always return the same result
Latency — an LLM call per command breaks the "live terminal" feel
Clean grading — the evaluation model can be given ground truth about what info was available at each step, rather than having to infer it


LLMs are used only at authoring time, to help generate plausible command outputs and fault descriptions for human review before they're locked into a scenario file.

Scenario object (the actual core asset)

Everything else is a renderer for this. Each scenario should be hand-authored (YAML/JSON), built backwards from a root cause:


Assert what broke
Define what a coherent system would look like under that breakage (what would ping, nslookup, systemctl status, etc. actually show)
Derive the persona's initial ticket description and their subjective responses to follow-ups from that same ground truth


Each scenario needs:


Symptom graph — root cause → observable symptom at each diagnostic command, including red herrings (plausible commands that return realistic but unhelpful output)
Discovery log — which commands were run, in what order, what's been revealed so far (this doubles as the evaluation transcript — don't build a separate logging mechanism)
Resolution state — not binary solved/unsolved. Track how: direct fix vs. correct diagnosis + appropriate escalation. Correct escalation must be able to score as a valid, high-scoring resolution — if "fix it yourself" always outscores "diagnose and escalate correctly," the rubric is teaching the wrong lesson, and that's an immediate credibility problem with anyone in IT reviewing this portfolio piece.


Scope for v1


System templates: start with desktop and server only. More templates are a roadmap item, not a v1 requirement.
Scenario categories: start with 2 (e.g. DNS, account lockout), each with a known, bounded command set (5–8 commands) and templated outputs. Grow incrementally — do not try to build a general-purpose shell simulator.
Tool/command list: tightly constrained to universal fundamentals (ping, nslookup/dig, ipconfig/ifconfig, traceroute, systemctl status, cat /etc/resolv.conf, etc.) — enough to cover the majority of realistic diagnostic situations, not exhaustive coverage.


Grading


Per-ticket-lifecycle evaluation (not per-message), run by the grading model after close or at defined milestones.
Rubric inputs: communication quality/tone across the thread, technical accuracy, time-to-first-response, whether the right clarifying questions were asked, whether the loop was closed with the user, and diagnostic path efficiency — actual command sequence logged against the scenario's known-optimal path.
Escalation handling must be scored as described above: appropriate escalation = valid resolution, not a fallback or partial failure.


Explicit non-goals / deferred (do not build into v1)


Docker-based live system images for remote-access-style tickets (Phase 2 stretch goal — would cut into the model pool available for narrative/persona, and needs lead time to load)
LLM-generated scenarios at runtime (authoring-time LLM assistance only, per above)
PSA feature parity in the ticketing layer
More than 2 system templates or more than 2 scenario categories at launch


When in doubt about scope, default to the smaller v1 footprint described here and note the larger version as a roadmap item rather than building it.

Working style for this project


Phased, incremental scope discipline. Ship the smallest coherent version of each layer before expanding it.
This is a portfolio piece for entry-level help desk / SOC job applications — code quality and architecture decisions should be ones the operator can confidently explain and defend in an interview ("walk me through a technical decision you made"), not just ones that work.
Operator is comfortable with the underlying systems conceptually and is not aiming for a software dev role — prioritize getting things working correctly and explainably over idiomatic-for-its-own-sake code golf. Comments/structure should favor "I can explain why this works" over cleverness.