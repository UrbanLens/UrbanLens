from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from functools import singledispatchmethod
import logging
import re
from typing import Any, Generic, TypeVar

import tiktoken

from urbanlens.dashboard.services.ai.message import MessageQueue
from urbanlens.dashboard.services.ai.meta import (
    FORMATTING,
    INSTRUCTIONS,
    MAX_TOKENS,
    PROJECT_DESCRIPTION,
    SHORTEST_MESSAGE,
)

logger = logging.getLogger(__name__)

Response = TypeVar("Response")


class LLMGateway[Response](ABC):
    _model: str | None
    _api_url: str | None
    _api_key: str | None
    extend: bool
    _token_count: dict[str, int]
    formatting: str
    instructions: str
    project_description: str
    max_tokens: int = MAX_TOKENS

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        api_url: str | None = None,
        formatting: str = FORMATTING,
        instructions: str = INSTRUCTIONS,
        project_description: str = PROJECT_DESCRIPTION,
        **kwargs,
    ):
        self._token_count = {"sent": 0, "received": 0}
        self.formatting = formatting
        self.instructions = instructions
        self.project_description = project_description
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.setup(**kwargs)

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @api_key.setter
    def api_key(self, value: str | None):
        self._api_key = value

    @property
    def model(self) -> str:
        if not self._model:
            return "gpt-5-nano"
        return self._model

    @model.setter
    def model(self, value: str | None):
        self._model = self._lookup_model(value)

    @property
    def api_url(self) -> str | None:
        return self._api_url

    @api_url.setter
    def api_url(self, value: str | None):
        self._api_url = value

    @property
    def sent_tokens(self) -> int:
        """
        Number of tokens for sentences in the LLMGateway instance.

        Returns:
            int:
                The number of tokens for sentences in the LLMGateway instance.

        """
        return self._token_count["sent"]

    @property
    def received_tokens(self) -> int:
        """
        Number of received tokens.

        Returns:
            int:
                The number of received tokens.

        """
        return self._token_count["received"]

    @property
    def tokens(self) -> int:
        """
        Total number of tokens, calculated as the sum of tokens sent and tokens received.

        Returns:
            int:
                The total number of tokens.

        Examples::
            If self._token_count is {'sent': 100, 'received': 50}, calling tokens will return 150.

        """
        return self._token_count["sent"] + self._token_count["received"]

    @property
    def cost(self) -> Decimal:
        """
        Calculates the total cost for the tokens sent and received based on the model's costs per thousand tokens.

        Returns the total cost for the tokens calculated based on the model's costs.

        Returns:
            Decimal:
                The total cost for the tokens sent and received.

        Examples::
            >>> doc_gen = LLMGateway(...)
            >>> doc_gen.send_tokens(100)
            >>> doc_gen.receive_tokens(50)
            >>> doc_gen.cost
            Decimal('0.15')

        """
        match self.model:
            case "gpt-5.2":
                cost_per_thousand_sent = Decimal("0.00175")
                cost_per_thousand_received = Decimal("0.014")
            case "gpt-5-mini":
                cost_per_thousand_sent = Decimal("0.00025")
                cost_per_thousand_received = Decimal("0.002")
            case "gpt-5-nano":
                cost_per_thousand_sent = Decimal("0.00005")
                cost_per_thousand_received = Decimal("0.0004")
            case _:
                logger.warning("Model not recognized. Using default costs.")
                cost_per_thousand_sent = Decimal("0.01")
                cost_per_thousand_received = Decimal("0.03")

        sent_cost = self.sent_tokens * cost_per_thousand_sent / 1000
        received_cost = self.received_tokens * cost_per_thousand_received / 1000
        return round(sent_cost + received_cost, 2)

    @singledispatchmethod
    def send_tokens(self, count: Any):
        """
        Annotates the number of tokens sent and updates the token count accordingly.

            Args:
                count (Any):
                    The number of tokens to be sent.

        """
        raise NotImplementedError

    @send_tokens.register
    def _(self, count: int):
        """
        Annotates the number of tokens sent and updates the token count accordingly.

            Args:
                count (int):
                    The number of tokens to be sent.

        """
        self._token_count["sent"] += count
        logger.debug("Sent %s tokens. Total sent: %s", count, self._token_count["sent"])

    @send_tokens.register
    def _(self, prompt: str):
        """
        Processes the prompt to calculate and send tokens.

            Args:
                prompt (str):
                    The prompt for which tokens are to be calculated and sent.

        """
        count = self.calculate_tokens(prompt)
        self._token_count["sent"] += count
        logger.debug("Sent %s tokens. Total sent: %s", count, self._token_count["sent"])

    @send_tokens.register
    def _(self, messages: MessageQueue):
        """
        Processes the messages to calculate and send tokens.

            Args:
                messages (MessageQueue):
                    The messages to be processed for token calculation and sent.

        """
        count = self.calculate_combined_tokens(messages)
        self._token_count["sent"] += count
        logger.debug("Sent %s tokens. Total sent: %s", count, self._token_count["sent"])

    @singledispatchmethod
    def receive_tokens(self, count: int):
        """
        Updates the count of received tokens and logs the information.

            Args:
                count (int):
                    The number of tokens received.

        """
        self._token_count["received"] += count
        logger.debug("Received %s tokens. Total received: %s", count, self._token_count["received"])

    @receive_tokens.register
    def _(self, prompt: str):
        """
        Process the prompt to calculate tokens and update the token count accordingly.

            Args:
                prompt (str):
                    The text prompt for which tokens are to be calculated.

        """
        count = self.calculate_tokens(prompt)
        self._token_count["received"] += count
        logger.debug("Received %s tokens. Total received: %s", count, self._token_count["received"])

    def setup(self, **kwargs):
        """
        Perform any necessary setup for the AI model.

        This method can be overridden by child classes to perform any necessary setup for the AI model.
        """
        logger.debug("LLMGateway setup not defined in subclass")

    def _lookup_model(self, model_name: str | None) -> str | None:
        if not model_name:
            return None

        return model_name.lower()

    def calculate_tokens(self, prompt: str) -> int:
        """
        Calculate the exact number of tokens in a given text prompt using the tokenizer from the transformers library.

            Args:
                prompt (str):
                    The text prompt to calculate token count for.

            Returns:
                int: The exact token count.

        """
        try:
            encoding = tiktoken.encoding_for_model(self.model)
            tokens = encoding.encode(prompt)
        except KeyError:
            logger.debug("tiktoken does not know model %s; falling back to o200k_base encoding", self.model)
            encoding = tiktoken.get_encoding("o200k_base")
            tokens = encoding.encode(prompt)

        return len(tokens)

    def calculate_combined_tokens(self, messages: MessageQueue | list[dict[str, str]]) -> int:
        """
        Calculate the exact number of tokens in a combined prompt.

            Args:
                messages (dict):
                    A dictionary of messages to be used for chat completion.

            Returns:
                int:
                    The exact token count for the given prompt.

        """
        prompt = "\n\n".join([message["content"] for message in messages])
        return self.calculate_tokens(prompt)

    def prepare_system_prompt(self, **kwargs) -> str:
        """
        Prepare a text prompt for the AI model to use as the system prompt.

        This can be overridden by child classes to include specific prompt formatting, if desired, but
        should usually be changed by passing custom instructions or a project description to the constructor.

        Returns:
            str:
                The prepared text prompt.

        """
        prompt = self.project_description
        if self.formatting:
            prompt += f"\n\n<FORMATTING>{self.formatting}</FORMATTING>"
        if self.instructions:
            prompt += f"\n\n<INSTRUCTIONS>{self.instructions}</INSTRUCTIONS>"
        return prompt

    def construct_messages(self, prompt: str) -> MessageQueue:
        """
        Construct a list of messages to be used for chat completion.

        Args:
            prompt (str):
                The text prompt to be used for chat completion.

        Returns:
            list[dict]:
                A list of messages to be used for chat completion.

        """
        queue = MessageQueue()
        system_prompt = self.prepare_system_prompt()

        # Truncate system prompt if it somehow exceeds the budget, leaving room for user input.
        system_budget = self.max_tokens - SHORTEST_MESSAGE * 2
        system_tokens = queue.estimate_tokens(system_prompt)
        if system_tokens > system_budget:
            # ~4 chars per token as a rough estimate
            system_prompt = system_prompt[: system_budget * 4]
            logger.warning("System prompt truncated to fit token budget (was ~%d tokens)", system_tokens)

        queue.add_message(system_prompt, role="system")

        used = queue.estimate_tokens()
        user_budget = self.max_tokens - used - SHORTEST_MESSAGE
        if user_budget <= 0:
            logger.warning("System prompt consumes entire token budget; user prompt skipped")
            return queue

        user_tokens = queue.estimate_tokens(prompt)
        if user_tokens - used > user_budget:
            prompt = prompt[: user_budget * 4]
            logger.warning("User prompt truncated to fit token budget")

        queue.add_message(prompt, role="user")
        return queue

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        """
        Send a prompt to the AI model and return the answer within its response.

        The prompt is scanned for injection patterns before being sent. If a
        high-confidence injection is detected (risk score >= 0.3) the sanitized
        version is used instead and a warning is logged.

        Args:
            prompt (str): The prompt to send to the AI model.
            kwargs: Additional keyword arguments that may be used for specific implementations.

        Returns:
            str | None: The answer from the AI model.

        """
        from urbanlens.dashboard.services.ai.scanner import scan as _scan_injection

        scan_result = _scan_injection(prompt, source="user")
        if scan_result.risk_score >= 0.3:
            logger.warning(
                "Prompt injection risk=%.2f for model '%s'; sending sanitized prompt",
                scan_result.risk_score,
                self.model,
            )
            prompt = scan_result.sanitized

        try:
            queue = self.construct_messages(prompt)
        except ValueError:
            logger.warning("Prompt exceeds token limit for model '%s'; skipping AI call", self.model)
            return None
        self.send_tokens(queue)

        if response := self._get_response(queue):
            if message := self._parse_response(response):
                self.receive_tokens(message)
                answer = self._parse_answer(message)
                if not answer:
                    logger.error("No answer from message queue: %s", queue)
                return answer

        return None

    @abstractmethod
    def _get_response(self, message_queue: MessageQueue) -> Response | None:
        """
        Send the message queue to the AI gateway, and return the response it provides, unmodified.

            Args:
                message_queue (MessageQueue):
                    The message queue to send to the AI gateway.

            Returns:
                Response (Generic type):
                    The response from the AI gateway.

        """
        raise NotImplementedError

    @abstractmethod
    def _parse_response(self, response: Response) -> str | None:
        """
        Parse the response from the AI Gateway and return the message body.

            Args:
                response (Response generic type):
                    The response from the AI Gateway.

            Returns:
                str:
                    The parsed response from the AI Gateway.

        """
        raise NotImplementedError

    def _parse_answer(self, message_content: str) -> str | None:
        """Parse the first <ANSWER> tag from the response body.

        Args:
            message_content: The content of the message to parse.

        Returns:
            The first extracted answer, or None.
        """
        answers = self._parse_answers(message_content)
        if answers:
            return answers[0]
        logger.error('No ANSWER in response from AI model "%s": Response: %s', self.model, message_content)
        return None

    def _parse_answers(self, message_content: str) -> list[str]:
        """Parse all <ANSWER>...</ANSWER> tags from the response body.

        Args:
            message_content: The content of the message to parse.

        Returns:
            List of extracted answer strings (stripped), may be empty.
        """
        try:
            return [
                m.strip()
                for m in re.findall(
                    r"[<\[]ANSWER:?[>\]](.*?)[<\[](?:[/\\]|END\s*)ANSWER[>\]]",
                    message_content,
                    re.DOTALL,
                )
                if m.strip()
            ]
        except (re.error, AttributeError) as exc:
            logger.exception(
                "Error parsing answers from response for model '%s': %s",
                self.model,
                exc,
            )
            return []

    def send_prompt_list(self, prompt: str, *, max_results: int | None = None, **kwargs) -> list[str]:
        """Like send_prompt but returns every ANSWER tag as a list.

        Useful when the AI is instructed to select multiple items and wraps
        each one in its own ANSWER tag.

        Args:
            prompt: The user prompt to send.
            max_results: Optional cap on the number of answers returned.

        Returns:
            List of answer strings (may be empty).
        """
        from urbanlens.dashboard.services.ai.scanner import scan as _scan_injection

        scan_result = _scan_injection(prompt, source="user")
        if scan_result.risk_score >= 0.3:
            logger.warning(
                "Prompt injection risk=%.2f for model '%s'; sending sanitized prompt",
                scan_result.risk_score,
                self.model,
            )
            prompt = scan_result.sanitized

        try:
            queue = self.construct_messages(prompt)
        except ValueError:
            logger.warning("Prompt exceeds token limit for model '%s'; skipping AI call", self.model)
            return []

        self.send_tokens(queue)

        if response := self._get_response(queue):
            if message := self._parse_response(response):
                self.receive_tokens(message)
                answers = self._parse_answers(message)
                if max_results is not None:
                    answers = answers[:max_results]
                return answers

        return []
