import importlib.util
import json
import unittest
from pathlib import Path
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/tool-result-causality.py"
spec = importlib.util.spec_from_file_location("tool_result_causality", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CausalityFixtureTests(unittest.TestCase):
    def test_design_is_36_balanced_cells(self):
        self.assertEqual(len(module.SCENARIOS) * len(module.CONDITIONS) * 3, 36)
        for scenario in module.SCENARIOS:
            requests = {module.SCENARIOS[scenario]["request"] for _ in module.CONDITIONS}
            self.assertEqual(len(requests), 1)

    def test_ordinary_context_and_tool_protocol_have_identical_payload_bytes(self):
        for scenario, spec in module.SCENARIOS.items():
            for load in ("short", "long"):
                u_messages = module.checkpoint_messages(scenario, "U" + load)
                t_messages = module.checkpoint_messages(scenario, "T" + load)
                u = u_messages[1]["content"][0]["text"]
                tool_call, tool_result = t_messages[1], t_messages[2]
                t = tool_result["content"][0]["text"]
                self.assertEqual(u.encode(), t.encode())
                self.assertEqual(tool_call["content"][0]["name"], spec["tool"])
                self.assertEqual(tool_result["isError"], spec["isError"])

    def test_error_protocols_are_paired_and_payload_includes_status(self):
        for scenario in ("permission", "service"):
            _, call, result = module.checkpoint_messages(scenario, "Tshort")
            self.assertEqual(call["content"][0]["id"], result["toolCallId"])
            self.assertTrue(result["isError"])
            self.assertIn("Error:", result["content"][0]["text"])
        self.assertIn("未改变任务状态", module.SCENARIOS["permission"]["fact"])

    def test_long_context_adds_only_fixed_csv_noise_before_fact(self):
        for scenario, spec in module.SCENARIOS.items():
            short = module.checkpoint_messages(scenario, "Ushort")[1]["content"][0]["text"]
            long = module.checkpoint_messages(scenario, "Ulong")[1]["content"][0]["text"]
            self.assertEqual(short, spec["fact"])
            self.assertEqual(long, module.CSV_NOISE + short)

    def test_case_id_maps_fault_kind_without_changing_cell_identity(self):
        self.assertEqual(module.condition_parts("permission-Tlong"), ("permission", "Tlong"))
        self.assertEqual(module.condition_parts("service-Ushort"), ("service", "Ushort"))
        with self.assertRaises(ValueError):
            module.condition_parts("permission-unknown")

    def test_actual_projector_keeps_shared_source_request_exactly_once(self):
        fixtures = [{"id": f"{scenario}-{condition}", "request": scenario_data["request"],
                     "messages": module.checkpoint_messages(scenario, condition)}
                    for scenario, scenario_data in module.SCENARIOS.items()
                    for condition in module.CONDITIONS]
        js = r"""
import { readFileSync } from 'node:fs'
import { createProjector } from './sandbox/reply-habits-experiment.mjs'
const fixtures = JSON.parse(readFileSync(0, 'utf8'))
const results = fixtures.map((fixture, index) => {
  const project = createProjector({ mode: 'A', seed: { messages: fixture.messages } })
  const messages = project({ sessionId: `test-${index}`, messages: [
    { role: 'system', content: [{ type: 'text', text: 'frozen system' }] },
    { role: 'user', id: 'actual-source', source: { kind: 'user' },
      content: [{ type: 'text', text: fixture.request }] },
    { role: 'user', id: 'catalog', source: { kind: 'skill-catalog' }, content: [] },
  ] }).messages
  const count = messages.filter(m => m.role === 'user' && m.source?.kind === 'user'
    && m.content?.some(b => b.text === fixture.request)).length
  return { id: fixture.id, count, systemPreserved: messages[0].content[0].text === 'frozen system' }
})
process.stdout.write(JSON.stringify(results))
"""
        run = subprocess.run(["node", "--input-type=module", "-e", js], cwd=module.ROOT,
                             input=json.dumps(fixtures, ensure_ascii=False), text=True,
                             capture_output=True, check=True)
        projected = json.loads(run.stdout)
        self.assertEqual(len(projected), 12)
        self.assertTrue(all(row["count"] == 1 and row["systemPreserved"] for row in projected))


if __name__ == "__main__":
    unittest.main()
