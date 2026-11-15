"""Asking for a different reminder time in your own words changes it, as TIME 7pm does
(Fareham resident, 15 Nov 2026: "Reminder, set to evening before 7pm")."""
import pytest

from app.services.reminders import _time_request

YES = [("Reminder, set to evening before 7pm", "19:00"), ("Can you remind me at 6pm instead?", "18:00"),
       ("change my reminder to 8.30pm", "20:30"), ("please make it 18:30", "18:30"),
       ("Could you send it at 7 please", "19:00"), ("remind me 9pm", "21:00")]
NO = ["What time do the bins get collected?", "the lorry came at 7am", "thanks!",
      "Reminder", "change my bins", "set it to 7pm or 8pm", "when do you remind me"]


@pytest.mark.parametrize("text,want", YES)
def test_asks_for_a_time(text, want):
    assert _time_request(text) == want


@pytest.mark.parametrize("text", NO)
def test_does_not(text):
    assert _time_request(text) is None
