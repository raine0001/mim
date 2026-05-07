import unittest

from core.program_registry_service import extract_program_projects_from_text
from core.program_registry_service import next_program_project
from core.program_registry_service import project_program_intent


PROGRAM_TEXT = """
PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION

GLOBAL EXECUTION AUTHORITY

This entire 12 program is pre-approved for autonomous execution.

Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION

OBJECTIVE:
Enforce that objectives cannot complete from planning text, broker artifacts, or task creation alone.

GOAL:
Completion must require real execution evidence.

SCOPE:
- handoff intake
- broker result handling

TASKS:
1. Inspect current completion conditions across gateway, handoff intake, autonomy/goal lifecycle, and status surfaces.
2. Identify every path where planning-only, broker-prep, model text, or non-executed artifacts can mark an objective complete.

SUCCESS CRITERIA:
- planning-only objective remains active/planning
- no status done from planning-only paths

Project_2_ID: MIM-DAY-02-INITIATIVE-ISOLATION

OBJECTIVE:
Prevent new initiatives from being overwritten, contaminated, or auto-rerouted into previously authorized initiatives.

GOAL:
Every incoming initiative stays bound to its own request, objective, task, and result lineage.

TASKS:
1. Trace request_id -> initiative_id -> objective_id -> task_id -> result path.
2. Patch precedence so explicit incoming INITIATIVE_ID wins over prior cached initiative continuation.

SUCCESS CRITERIA:
- explicit INITIATIVE_ID always wins over stale active initiative continuation

Project 14 - exted simulation training on MIM natural language learning 1,000,000 questions / interactions in each group

OBJECTIVE:
Run staged simulation training across leadership, initiative, project managment, social engagment, and software development and planning.

GOAL:
Run small batches first, optimize, then complete the full run.

TASKS:
1. Run small batches to start.
2. Check results and optimize.
3. Complete the full run.

SUCCESS CRITERIA:
- small batches complete before full run
- the next category starts automatically
"""


class ProgramRegistryServiceTests(unittest.TestCase):
    def test_extracts_full_project_contracts_from_program_text(self) -> None:
        projects = extract_program_projects_from_text(PROGRAM_TEXT)

        self.assertEqual(len(projects), 3)
        self.assertEqual(projects[0]["project_id"], "MIM-DAY-01-EXECUTION-BOUND-COMPLETION")
        self.assertEqual(projects[1]["project_id"], "MIM-DAY-02-INITIATIVE-ISOLATION")
        self.assertTrue(projects[2]["project_id"].startswith("MIM-DAY-14-EXTED-SIMULATION-TRAINING-ON-MIM-NATURAL-LANGUAGE-LEARNING"))
        self.assertIn("handoff intake", projects[0]["scope"])
        self.assertEqual(len(projects[0]["tasks"]), 2)
        self.assertIn("explicit INITIATIVE_ID always wins", projects[1]["success_criteria"][0])

    def test_project_program_intent_reconstructs_single_project_payload(self) -> None:
        project = extract_program_projects_from_text(PROGRAM_TEXT)[1]

        intent = project_program_intent("MIM-12-AUTONOMOUS-EVOLUTION", project)

        self.assertIn("PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION", intent)
        self.assertIn("INITIATIVE_ID: MIM-DAY-02-INITIATIVE-ISOLATION", intent)
        self.assertIn("TASKS:", intent)
        self.assertIn("1. Trace request_id -> initiative_id -> objective_id -> task_id -> result path.", intent)

    def test_next_program_project_returns_next_ordinal_project(self) -> None:
        projects = extract_program_projects_from_text(PROGRAM_TEXT)
        registry = {
            "active_program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
            "programs": [
                {
                    "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "projects": projects,
                }
            ],
        }

        next_project = next_program_project(registry, "MIM-DAY-01-EXECUTION-BOUND-COMPLETION")

        self.assertEqual(next_project.get("project_id"), "MIM-DAY-02-INITIATIVE-ISOLATION")


if __name__ == "__main__":
    unittest.main()