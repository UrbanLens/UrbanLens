"""*********************************************************************************************************************
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    METADATA:                                                                                                         *
*                                                                                                                      *
*        - File:    gateway.py                                                                                         *
*        - Path:    /dashboard/services/ai/gateway.py                                                                  *
*        - Project: urbanlens                                                                                          *
*        - Version: 1.0.0                                                                                              *
*        - Created: 2024-03-21                                                                                         *
*        - Author:  Jess Mann                                                                                          *
*        - Email:   jess@urbanlens.org                                                                               *
*        - Copyright (c) 2024 Urban Lens                                                                               *
*                                                                                                                      *
* -------------------------------------------------------------------------------------------------------------------- *
*                                                                                                                      *
*    LAST MODIFIED:                                                                                                    *
*                                                                                                                      *
*        2024-03-21     By Jess Mann                                                                                   *
*                                                                                                                      *
*********************************************************************************************************************"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from functools import singledispatchmethod
import logging
import re
from typing import Any, Generic, TypeVar

import tiktoken

from urbanlens.dashboard.services.ai.message import MessageQueue
from urbanlens.dashboard.services.ai.meta import FORMATTING, INSTRUCTIONS, MAX_TOKENS, PROJECT_DESCRIPTION

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
        Returns the number of tokens for sentences in the LLMGateway instance.

            Returns
        -------
                int:
                    The number of tokens for sentences in the LLMGateway instance.

        """
        return self._token_count["sent"]

    @property
    def received_tokens(self) -> int:
        """
        Returns the number of received tokens.

            Returns
        -------
                int:
                    The number of received tokens.

        """
        return self._token_count["received"]

    @property
    def tokens(self) -> int:
        """
        Returns the total number of tokens, calculated as the sum of tokens sent and tokens received.

            Returns
        -------
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

        Returns
        -------
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
        ----
                count (Any):
                    The number of tokens to be sent.

        """
        raise NotImplementedError

    @send_tokens.register
    def _(self, count: int):
        """
        Annotates the number of tokens sent and updates the token count accordingly.

            Args:
        ----
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
        ----
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
        ----
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
        ----
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
        ----
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
        ----
                prompt (str):
                    The text prompt to calculate token count for.

            Returns:
        -------
                int: The exact token count.

        """
        try:
            encoding = tiktoken.encoding_for_model(self.model)
            tokens = encoding.encode(prompt)
        except KeyError:
            logger.debug("KeyError when using model %s to calculate tokens", self.model)
            encoding = tiktoken.encoding_for_model("gpt-5-nano")
            tokens = encoding.encode(prompt)

        return len(tokens)

    def calculate_combined_tokens(self, messages: MessageQueue | list[dict[str, str]]) -> int:
        """
        Calculate the exact number of tokens in a combined prompt.

            Args:
        ----
                messages (dict):
                    A dictionary of messages to be used for chat completion.

            Returns:
        -------
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

        Raises:
            ValueError:
                If the prompt exceeds the maximum token limit.

        Returns:
            list[dict]:
                A list of messages to be used for chat completion.

        """
        queue = MessageQueue()
        system_prompt = self.prepare_system_prompt()
        queue.add_message(system_prompt, role="system")
        queue.add_message(prompt, role="user")

        return queue

    def send_prompt(self, prompt: str, **kwargs) -> str | None:
        """
        Send a prompt to the AI model and return the answer within its response.

        Args:
            prompt (str): The prompt to send to the AI model.
            kwargs: Additional keyword arguments that may be used for specific implementations.

        Returns:
            str | None: The answer from the AI model.

        """
        queue = self.construct_messages(prompt)
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
        ----
                message_queue (MessageQueue):
                    The message queue to send to the AI gateway.

            Returns:
        -------
                Response (Generic type):
                    The response from the AI gateway.

        """
        raise NotImplementedError

    @abstractmethod
    def _parse_response(self, response: Response) -> str | None:
        """
        Parse the response from the AI Gateway and return the message body.

            Args:
        ----
                response (Response generic type):
                    The response from the AI Gateway.

            Returns:
        -------
                str:
                    The parsed response from the AI Gateway.

        """
        raise NotImplementedError

    def _parse_answer(self, message_content: str) -> str | None:
        """
        Parse the <ANSWER> tag from the response body.

            Args:
        ----
                message_content (str):
                    The content of the message to parse.

            Returns:
        -------
                str | None:
                    The parsed answer from the response.

        """
        try:
            if match := re.search(r"[<\[]ANSWER:?[>\]](.*?)[<\[]([/\\]|END\s*)ANSWER[>\]]", message_content, re.DOTALL):
                return match.group(1).strip()
            logger.error('No ANSWER in response from AI model "%s": Response: %s', self.model, message_content)
        except Exception as e:
            logger.exception(
                "Error parsing answer from response for model '%s'. Respoonse: %s\nError: %s",
                self.model,
                message_content,
                e,
            )

        return None
