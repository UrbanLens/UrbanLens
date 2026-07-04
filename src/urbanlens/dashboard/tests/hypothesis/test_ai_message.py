"""Tests for the AI MessageQueue and token estimation utilities.

No database access - all tests exercise pure Python logic.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.ai.functions import estimate_combined_tokens, estimate_tokens
from urbanlens.dashboard.services.ai.message import AssistantMessage, MessageQueue, SystemMessage, UserMessage
from urbanlens.dashboard.services.ai.meta import MAX_TOKENS, SHORTEST_MESSAGE


_hyp = settings(max_examples=100, deadline=None)
_hyp_light = settings(max_examples=50, deadline=None)

# Strategy: ASCII printable text without leading/trailing whitespace
_word = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=122, blacklist_characters='\\`'),
    min_size=1,
    max_size=20,
)
_sentence = st.lists(_word, min_size=1, max_size=20).map(" ".join)


# -- estimate_tokens ------------------------------------------------------------

class EstimateTokensTests(TestCase):
    """estimate_tokens produces a non-negative integer approximation."""

    def test_empty_string_returns_zero(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_single_word_returns_one(self) -> None:
        self.assertEqual(estimate_tokens("hello"), 1)

    def test_two_words_returns_two(self) -> None:
        self.assertEqual(estimate_tokens("hello world"), 2)

    def test_punctuation_splits_into_multiple_tokens(self) -> None:
        result = estimate_tokens("hello,world")
        self.assertGreaterEqual(result, 2)

    def test_sentence_with_period_splits_period(self) -> None:
        result = estimate_tokens("Hello world.")
        self.assertGreaterEqual(result, 2)

    def test_whitespace_only_returns_zero(self) -> None:
        self.assertEqual(estimate_tokens("   "), 0)

    def test_multiple_spaces_treated_as_one_separator(self) -> None:
        self.assertEqual(estimate_tokens("a  b"), 2)

    def test_returns_integer(self) -> None:
        result = estimate_tokens("hello world")
        self.assertIsInstance(result, int)

    @given(_sentence)
    @_hyp
    def test_always_non_negative(self, text: str) -> None:
        self.assertGreaterEqual(estimate_tokens(text), 0)

    @given(st.text(alphabet=st.characters(min_codepoint=65, max_codepoint=90), min_size=1, max_size=200))
    @_hyp
    def test_returns_non_negative_for_any_text(self, text: str) -> None:
        result = estimate_tokens(text)
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    @given(st.lists(_word, min_size=2, max_size=10))
    @_hyp
    def test_more_words_means_more_or_equal_tokens(self, words: list[str]) -> None:
        shorter = " ".join(words[:-1])
        longer = " ".join(words)
        self.assertLessEqual(estimate_tokens(shorter), estimate_tokens(longer))


# -- estimate_combined_tokens ---------------------------------------------------

class EstimateCombinedTokensTests(TestCase):
    """estimate_combined_tokens sums token counts across all messages."""

    def test_empty_list_returns_zero(self) -> None:
        self.assertEqual(estimate_combined_tokens([]), 0)

    def test_single_message_matches_individual_estimate(self) -> None:
        msg: UserMessage = {"role": "user", "content": "hello world"}
        self.assertEqual(estimate_combined_tokens([msg]), estimate_tokens("hello world"))

    def test_two_messages_sums_their_tokens(self) -> None:
        msgs: list[UserMessage | AssistantMessage] = [
            {"role": "user", "content": "hello world"},
            {"role": "assistant", "content": "goodbye world"},
        ]
        expected = estimate_tokens("hello world") + estimate_tokens("goodbye world")
        self.assertEqual(estimate_combined_tokens(msgs), expected)

    def test_roles_do_not_affect_count(self) -> None:
        content = "same content here"
        user_msg: list[UserMessage] = [{"role": "user", "content": content}]
        sys_msg: list[SystemMessage] = [{"role": "system", "content": content}]
        self.assertEqual(estimate_combined_tokens(user_msg), estimate_combined_tokens(sys_msg))

    @given(st.lists(_sentence, min_size=0, max_size=5))
    @_hyp_light
    def test_total_is_sum_of_individual_estimates(self, contents: list[str]) -> None:
        msgs: list[UserMessage] = [{"role": "user", "content": c} for c in contents]
        expected = sum(estimate_tokens(c) for c in contents)
        self.assertEqual(estimate_combined_tokens(msgs), expected)


# -- MessageQueue.__init__ and add_message --------------------------------------

class MessageQueueInitTests(TestCase):
    """MessageQueue initialises with an empty list and the given max_tokens."""

    def test_starts_empty(self) -> None:
        q = MessageQueue()
        self.assertEqual(len(q.messages), 0)

    def test_default_max_tokens(self) -> None:
        q = MessageQueue()
        self.assertEqual(q.max_tokens, MAX_TOKENS)

    def test_custom_max_tokens(self) -> None:
        q = MessageQueue(max_tokens=500)
        self.assertEqual(q.max_tokens, 500)


class MessageQueueAddMessageTests(TestCase):
    """add_message stores typed dicts with the correct role."""

    def test_user_role_is_default(self) -> None:
        q = MessageQueue()
        q.add_message("hello")
        self.assertEqual(q.messages[0]["role"], "user")

    def test_system_role_stored_correctly(self) -> None:
        q = MessageQueue()
        q.add_message("sys prompt", role="system")
        self.assertEqual(q.messages[0]["role"], "system")

    def test_assistant_role_stored_correctly(self) -> None:
        q = MessageQueue()
        q.add_message("response", role="assistant")
        self.assertEqual(q.messages[0]["role"], "assistant")

    def test_content_is_preserved(self) -> None:
        q = MessageQueue()
        q.add_message("exact content here")
        self.assertEqual(q.messages[0]["content"], "exact content here")

    def test_multiple_messages_accumulate(self) -> None:
        q = MessageQueue()
        q.add_message("first")
        q.add_message("second")
        self.assertEqual(len(q.messages), 2)

    def test_too_long_message_raises_value_error(self) -> None:
        # With max_tokens=SHORTEST_MESSAGE, any non-empty message will exceed the limit.
        q = MessageQueue(max_tokens=SHORTEST_MESSAGE)
        long_words = " ".join(["word"] * (SHORTEST_MESSAGE + 1))
        with self.assertRaises(ValueError):
            q.add_message(long_words)

    def test_error_message_contains_token_info(self) -> None:
        q = MessageQueue(max_tokens=SHORTEST_MESSAGE)
        long_words = " ".join(["word"] * (SHORTEST_MESSAGE + 1))
        with self.assertRaises(ValueError) as ctx:
            q.add_message(long_words)
        self.assertIn(str(SHORTEST_MESSAGE), str(ctx.exception))

    @given(st.sampled_from(["user", "system", "assistant"]))
    @_hyp
    def test_any_valid_role_is_accepted(self, role: str) -> None:
        q = MessageQueue()
        q.add_message("test", role=role)  # type: ignore[arg-type]
        self.assertEqual(q.messages[0]["role"], role)


# -- MessageQueue sequence protocol --------------------------------------------

class MessageQueueSequenceTests(TestCase):
    """MessageQueue supports __iter__, __len__, __getitem__, __setitem__, __delitem__."""

    def _filled(self) -> MessageQueue:
        q = MessageQueue()
        q.add_message("first", role="user")
        q.add_message("second", role="assistant")
        return q

    def test_len_returns_message_count(self) -> None:
        q = self._filled()
        self.assertEqual(len(q), 2)

    def test_len_of_empty_queue_is_zero(self) -> None:
        self.assertEqual(len(MessageQueue()), 0)

    def test_getitem_returns_correct_message(self) -> None:
        q = self._filled()
        self.assertEqual(q[0]["content"], "first")
        self.assertEqual(q[1]["content"], "second")

    def test_setitem_replaces_message(self) -> None:
        q = self._filled()
        replacement: SystemMessage = {"role": "system", "content": "replaced"}
        q[0] = replacement
        self.assertEqual(q.messages[0]["content"], "replaced")

    def test_delitem_removes_message(self) -> None:
        q = self._filled()
        del q[0]
        self.assertEqual(len(q), 1)
        self.assertEqual(q.messages[0]["content"], "second")

    def test_iter_yields_all_messages_in_order(self) -> None:
        q = self._filled()
        contents = [msg["content"] for msg in q]
        self.assertEqual(contents, ["first", "second"])

    def test_str_is_non_empty(self) -> None:
        q = self._filled()
        self.assertTrue(str(q))

    def test_repr_is_non_empty(self) -> None:
        q = self._filled()
        self.assertTrue(repr(q))


# -- MessageQueue.estimate_tokens -----------------------------------------------

class MessageQueueEstimateTokensTests(TestCase):
    """estimate_tokens on the queue returns the combined token count."""

    def test_empty_queue_returns_zero(self) -> None:
        self.assertEqual(MessageQueue().estimate_tokens(), 0)

    def test_with_messages_matches_sum_of_content(self) -> None:
        q = MessageQueue()
        q.add_message("hello world", role="user")
        expected = estimate_tokens("hello world")
        self.assertEqual(q.estimate_tokens(), expected)

    def test_additional_prompt_adds_to_count(self) -> None:
        q = MessageQueue()
        base = q.estimate_tokens()
        with_prompt = q.estimate_tokens("extra words here")
        self.assertGreater(with_prompt, base)

    def test_none_additional_prompt_matches_base(self) -> None:
        q = MessageQueue()
        q.add_message("some text")
        self.assertEqual(q.estimate_tokens(), q.estimate_tokens(None))

    @given(_sentence)
    @_hyp
    def test_additional_prompt_is_always_non_negative_increment(self, prompt: str) -> None:
        q = MessageQueue()
        base = q.estimate_tokens()
        total = q.estimate_tokens(prompt)
        self.assertGreaterEqual(total, base)
