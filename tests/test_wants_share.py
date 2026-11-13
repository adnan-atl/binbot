"""Asking how to pass Binjamin on, in your own words, gets the forwardable message straight
away, as if you'd typed "share". Talking about someone else sharing doesn't."""
import pytest

from app.services.reminders import WANTS_SHARE_RE

YES = ["How can I refer you?", "how do i share this with my neighbours", "Can I invite my mum?",
       "I'd like to recommend you to friends", "I want to share Binjamin with my street",
       "Could I pass you on to my sister?", "Is there a way to refer someone",
       "how can i tell my friends about you", "Where do I send this to my neighbour?",
       "what's the best way to share it?", "can we invite our neighbours"]
NO = ["Who referred me?", "My neighbour shared it with me", "I was invited by a friend",
      "thanks for sharing", "Shared bins in our block", "Can I change my bins?",
      "how do I stop reminders", "What is tomorrow?", "my mate recommended you"]


@pytest.mark.parametrize("text", YES)
def test_asks_to_share(text):
    assert WANTS_SHARE_RE.search(text)


@pytest.mark.parametrize("text", NO)
def test_does_not(text):
    assert not WANTS_SHARE_RE.search(text)
