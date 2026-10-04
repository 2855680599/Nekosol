# Canonical Semantic Ownership Registry Proposal

This registry is source-owned here; it does not mutate an existing production registry. No
generic registry existed in the inspected deployment. Apply only with the registry owner's
approval, through that registry's own maintenance authority.

**C10 correction.** Before the single-database merge this proposal named **three separate store
files**. The runtime is now **one** canonical database, so every row below points at the same
file. One database is not one universal owner: each owner keeps its own tables, its own writer
fence and its own write guards inside that file.

Layout: `life-supply.single-db.v1`, canonical file `./data/supply/life_supply.sqlite`.

| Canonical fact type | Unique owner | Store |
|---|---|---|
| `permission_grant` | Governance | `./data/supply/life_supply.sqlite` |
| `permission_reservation` | Governance | `./data/supply/life_supply.sqlite` |
| `authority_operation` | Governance | `./data/supply/life_supply.sqlite` |
| `personal_workspace_capability` | Workspace | `./data/supply/life_supply.sqlite` |
| `open_inquiry` | Workspace | `./data/supply/life_supply.sqlite` |
| `inquiry_proposal` | Workspace | `./data/supply/life_supply.sqlite` |
| `inquiry_admission_result` | Workspace | `./data/supply/life_supply.sqlite` |
| `managed_artifact` | ManagedArtifact | `./data/supply/life_supply.sqlite` |
| `artifact_version` | ManagedArtifact | `./data/supply/life_supply.sqlite` |
| `artifact_operation` | ManagedArtifact | `./data/supply/life_supply.sqlite` |
| `artifact_projection_binding` | ManagedArtifact | `./data/supply/life_supply.sqlite` |
| `artifact_effect_receipt` | ManagedArtifact | `./data/supply/life_supply.sqlite` |

Rules: `writers == {canonical_owner}`; the proposal bus is the only channel.

Not owned or written here: Decision, Activity, Action/ActionResult, World, Memory, Preference,
Interest, Habit, Goal, Relationship, Continuation. Continuation remains with the Activity owner.
No candidate is produced merely by the existence of a Workspace.

Status of the production registry: `CANONICAL_OWNERSHIP_REGISTRY.json`, sha256
`57ddd40c12e4db9a8ad881ea138d10c577e50310761f0f5bc57237d11e08b373`, was **not modified** by this
batch and does not yet carry these three owners. Its 18 state types are
Evidence, Experience, Interpretation, Lesson, Expectation, RelationshipState, AffectState,
ExplicitUserPreference, SelfPreference, ActivityAttention, BeliefState, ActionResult, Goals,
ContactIntention, DecisionIntent, ExpressionPlan, SurfaceRealization, VisibleReply — none of them
collides with Governance, Workspace or ManagedArtifact.