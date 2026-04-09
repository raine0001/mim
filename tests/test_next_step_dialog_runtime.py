import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core import app as app_module


class NextStepDialogRuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        app_module.app.state.self_awareness_task = None
        app_module.app.state.next_step_dialog_task = None

    async def asyncTearDown(self) -> None:
        await app_module._stop_background_runtime_tasks()

    async def test_start_background_runtime_tasks_starts_next_step_dialog_responder(self) -> None:
        blocker = asyncio.Event()

        async def fake_self_awareness() -> None:
            await blocker.wait()

        async def fake_next_step_dialog() -> None:
            await blocker.wait()

        with (
            patch.object(app_module, "mim_self_awareness_service", side_effect=fake_self_awareness),
            patch.object(app_module, "run_next_step_dialog_responder_loop", side_effect=fake_next_step_dialog),
        ):
            app_module._start_background_runtime_tasks()

            self.assertIsNotNone(app_module.app.state.self_awareness_task)
            self.assertIsNotNone(app_module.app.state.next_step_dialog_task)
            self.assertEqual(
                app_module.app.state.next_step_dialog_task.get_name(),
                "mim-next-step-dialog-responder",
            )

            blocker.set()
            await app_module._stop_background_runtime_tasks()

            self.assertIsNone(app_module.app.state.self_awareness_task)
            self.assertIsNone(app_module.app.state.next_step_dialog_task)

    async def test_shutdown_clears_next_step_dialog_task_reference(self) -> None:
        responder_task = asyncio.create_task(asyncio.sleep(60), name="mim-next-step-dialog-responder")
        self_awareness_task = asyncio.create_task(asyncio.sleep(60), name="mim-self-awareness-service")
        app_module.app.state.next_step_dialog_task = responder_task
        app_module.app.state.self_awareness_task = self_awareness_task

        with patch.object(app_module, "shutdown_workspace_monitoring_runtime", new=AsyncMock()):
            await app_module.on_shutdown()

        self.assertIsNone(app_module.app.state.next_step_dialog_task)
        self.assertIsNone(app_module.app.state.self_awareness_task)
        self.assertTrue(responder_task.cancelled())
        self.assertTrue(self_awareness_task.cancelled())