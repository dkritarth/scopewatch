import unittest
import json
import os
from src.pipeline import AuditPipeline
from src.cot_capture import DefaultReasoningCapture
from src.scope_auditor import ScopeAuditor, MockAuditorBackend
from src.models import TaskScope, ScopeClassificationEnum


def task_scope_from_fixture(fixture_dict: dict) -> TaskScope:
    """Build a TaskScope from a fixture dict without mutating the original."""
    return TaskScope(
        task_description=fixture_dict["description"],
        allowed_paths=fixture_dict.get("allowed_paths", []),
        allowed_tools=fixture_dict.get("allowed_tools", []),
        blocked_paths=fixture_dict.get("blocked_paths", []),
    )


class TestAuditPipeline(unittest.TestCase):
    def setUp(self):
        self.capture = DefaultReasoningCapture()
        self.auditor = ScopeAuditor(MockAuditorBackend())
        self.pipeline = AuditPipeline(self.capture, self.auditor)

        base_dir = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(base_dir, "fixtures", "tasks.json"), "r") as f:
            self.tasks = json.load(f)
        with open(os.path.join(base_dir, "fixtures", "in_scope_reasoning.json"), "r") as f:
            self.in_scope = json.load(f)
        with open(os.path.join(base_dir, "fixtures", "out_of_scope_reasoning.json"), "r") as f:
            self.out_of_scope = json.load(f)

    def test_in_scope_pipeline(self):
        scope = task_scope_from_fixture(self.tasks[0])
        reasoning_data = self.in_scope[0]
        result = self.pipeline.process(
            raw_output=reasoning_data["raw_output"],
            source_model="model",
            task_scope=scope,
            metadata=reasoning_data.get("metadata"),
        )
        self.assertEqual(result.status, ScopeClassificationEnum.IN_SCOPE)

    def test_out_of_scope_pipeline(self):
        scope = task_scope_from_fixture(self.tasks[0])
        reasoning_data = self.out_of_scope[0]
        result = self.pipeline.process(
            raw_output=reasoning_data["raw_output"],
            source_model="model",
            task_scope=scope,
            metadata=reasoning_data.get("metadata"),
        )
        self.assertEqual(result.status, ScopeClassificationEnum.OUT_OF_SCOPE)

    def test_no_reasoning(self):
        scope = task_scope_from_fixture(self.tasks[0])
        result = self.pipeline.process(
            raw_output="Just some normal text with no tags",
            source_model="model",
            task_scope=scope,
        )
        self.assertEqual(result.status, ScopeClassificationEnum.HOLD)
