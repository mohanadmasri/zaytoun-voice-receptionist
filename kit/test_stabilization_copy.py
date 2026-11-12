"""Patient-facing copy regressions for both clinic clients and booking review."""
import unittest
from pathlib import Path
from unittest.mock import patch

from actions.approval import prepare_booking_action
from agent.state import create_initial_state
from ui import app_ui


class PatientCopyTests(unittest.TestCase):
    def test_clinic_clients_do_not_expose_technical_diagnostics(self):
        web = Path(__file__).parent / "ui" / "web"
        self.assertNotIn('id="diagnostics"', (web / "index.html").read_text(encoding="utf-8"))
        self.assertFalse(any(c["type"] == "accordion" for c in app_ui.demo.config["components"]))
        outputs = app_ui.show({"reply": "Hello", "tool_calls": [{"tool": "save_patient"}]}, [])
        self.assertNotIn("save_patient", repr(outputs))

    def test_booking_review_excludes_persistence_copy(self):
        state = create_initial_state("copy-test")
        state.update(service_id="dental_checkup", selected_slot_id="copy-slot",
                     requested_date="2099-11-20",
                     patient_name="Lina Mansour", patient_phone="0599000999",
                     confirmed_phone="0599000999", provider_choice="specific",
                     proposed_slots=[{"slot_id": "copy-slot", "provider_id": "dr_nassar"}])
        with patch("actions.approval.slot_matches_offer", return_value=True), patch(
            "actions.approval.booking_summary", return_value="Appointment details."
        ):
            for language in ("ar", "en"):
                with self.subTest(language=language):
                    state["language"] = language
                    pending = prepare_booking_action(state)
                    self.assertEqual(pending["summary"], "Appointment details.")
                    self.assertEqual(pending["memory"]["preferred_provider_id"], "dr_nassar")
