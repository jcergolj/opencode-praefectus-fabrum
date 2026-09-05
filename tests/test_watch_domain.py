import unittest

from watch_test_support import watch


class SessionStateMachineTests(unittest.TestCase):
    def test_new_session_starts_idle(self):
        machine = watch.SessionStateMachine()

        self.assertEqual(machine.status, watch.SessionStatus.IDLE)

    def test_allowed_transitions_follow_the_session_lifecycle(self):
        machine = watch.SessionStateMachine()

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("IDLE"))
        self.assertEqual(machine.status, watch.SessionStatus.IDLE)

    def test_unknown_transitions_are_rejected_without_changing_state(self):
        machine = watch.SessionStateMachine()

        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("IDLE"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertEqual(machine.status, watch.SessionStatus.NEEDS_APPROVAL)

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertFalse(machine.transition_to("COMPLETED"))
        self.assertEqual(machine.status, watch.SessionStatus.WORKING)

        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("IDLE"))
        self.assertEqual(machine.status, watch.SessionStatus.IDLE)

    def test_attention_states_can_transition_to_every_state(self):
        machine = watch.SessionStateMachine()

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("IDLE"))

        self.assertTrue(machine.transition_to("WORKING"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("WAITING"))
        self.assertTrue(machine.transition_to("NEEDS_APPROVAL"))
        self.assertTrue(machine.transition_to("IDLE"))


if __name__ == "__main__":
    unittest.main()
